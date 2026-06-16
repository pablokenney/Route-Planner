"""Phase 2 — the real loop generator (PLAN.md §4).

Fires N concurrent round_trip calls at GraphHopper (flex `run` profile, varied seed),
scores candidates with a DISTANCE-DOMINANT, distance-aware objective, de-duplicates
overlapping loops, optionally refines toward the target, and returns a ranked top-K.

Distance-aware road scoring (the key Phase 1 follow-through): REACHABILITY.md showed the
constrained network thins with range (run/raw 0.75 -> 0.44), so longer loops MUST use more
last-resort connectors (tertiary/unclassified/service) to make up mileage. The road
penalty therefore charges only the EXCESS last-resort fraction above a distance-scaled
expectation — a long loop is not penalized for doing what the network requires.
"""
from __future__ import annotations

import asyncio
import math
import os
import random
from dataclasses import dataclass, field

import httpx

GH = os.environ.get("GH_URL", "http://localhost:8989")
HOME = (40.195016, -77.199929)  # lat, lon
MI = 1609.344

# Road-class buckets for the road-mix penalty.
LAST_RESORT = {"tertiary", "unclassified", "road"}  # penalized last-resort
SERVICE = "service"                                  # mild last-resort (half weight)
PREFERRED = {"footway", "path", "cycleway", "pedestrian", "living_street"}

# Scoring weights — distance DOMINATES; road-mix and flatness are gentle tie-breakers
# (their max contributions, 0.05 and 0.02, are far below typical distance errors of
# 0.02-0.13, so a closer loop reliably ranks ahead of a farther but "cleaner" one).
W_DIST = 1.0
W_ROAD = 0.05
W_FLAT = 0.02
FLAT_CAP_M_PER_MI = 50.0  # gain/mi that maps to a full flatness penalty of 1.0

# Distance-aware expected last-resort fraction (linear 3mi->11mi, then clamped).
_EXP_LO_MI, _EXP_HI_MI = 3.0, 11.0
_EXP_LO_FRAC, _EXP_HI_FRAC = 0.05, 0.25


def expected_lastresort_frac(target_mi: float) -> float:
    t = (target_mi - _EXP_LO_MI) / (_EXP_HI_MI - _EXP_LO_MI)
    t = max(0.0, min(1.0, t))
    return _EXP_LO_FRAC + t * (_EXP_HI_FRAC - _EXP_LO_FRAC)


# ----------------------------------------------------------------------------- geometry
def _hav(p, q) -> float:
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [p[1], p[0], q[1], q[0]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _length_by_class(path: dict) -> dict[str, float]:
    cs = path["points"]["coordinates"]
    out: dict[str, float] = {}
    for fr, to, cls in path.get("details", {}).get("road_class", []):
        seg = sum(_hav(cs[i], cs[i + 1]) for i in range(fr, to))
        k = str(cls).lower()
        out[k] = out.get(k, 0.0) + seg
    return out


def _cells(coords, size_deg: float = 0.0005) -> set[tuple[int, int]]:
    """~55 m grid cells covering the loop — used for overlap/similarity de-dup."""
    return {(round(c[1] / size_deg), round(c[0] / size_deg)) for c in coords}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


# --------------------------------------------------------------------------- candidate
@dataclass
class Candidate:
    seed: int
    gen_distance_m: int       # the round_trip.distance used to produce it
    distance_m: float
    ascend_m: float
    descend_m: float
    coords: list              # [lon, lat, (ele)]
    length_by_class: dict = field(default_factory=dict)
    cells: set = field(default_factory=set)
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)

    @property
    def distance_mi(self) -> float:
        return self.distance_m / MI

    def road_mix(self) -> dict[str, float]:
        total = sum(self.length_by_class.values()) or 1.0
        mix = {k: round(100 * v / total, 1) for k, v in self.length_by_class.items()}
        return dict(sorted(mix.items(), key=lambda kv: kv[1], reverse=True))

    def lastresort_frac(self) -> float:
        total = sum(self.length_by_class.values()) or 1.0
        lr = sum(v for k, v in self.length_by_class.items() if k in LAST_RESORT)
        lr += 0.5 * self.length_by_class.get(SERVICE, 0.0)
        return lr / total


def _make_candidate(seed: int, gen_distance_m: int, path: dict) -> Candidate:
    coords = path["points"]["coordinates"]
    c = Candidate(
        seed=seed, gen_distance_m=gen_distance_m,
        distance_m=float(path.get("distance", 0.0)),
        ascend_m=float(path.get("ascend", 0.0)),
        descend_m=float(path.get("descend", 0.0)),
        coords=coords,
    )
    c.length_by_class = _length_by_class(path)
    c.cells = _cells(coords)
    return c


def score_candidate(c: Candidate, target_m: float, target_mi: float) -> None:
    d_err = abs(c.distance_m - target_m) / target_m
    expected = expected_lastresort_frac(target_mi)
    road_pen = max(0.0, c.lastresort_frac() - expected) / (1.0 - expected)
    gain_per_mi = c.ascend_m / max(c.distance_mi, 0.1)
    flat = min(1.0, gain_per_mi / FLAT_CAP_M_PER_MI)
    c.score = W_DIST * d_err + W_ROAD * road_pen + W_FLAT * flat
    c.breakdown = {
        "distance_err": round(d_err, 4),
        "road_penalty": round(road_pen, 4),
        "lastresort_frac": round(c.lastresort_frac(), 4),
        "expected_lastresort": round(expected, 4),
        "flatness": round(flat, 4),
    }


# --------------------------------------------------------------------------- GH calls
async def _round_trip(client: httpx.AsyncClient, seed: int, gen_distance_m: int, start):
    try:
        r = await client.get(f"{GH}/route", params={
            "point": f"{start[0]},{start[1]}", "profile": "run", "algorithm": "round_trip",
            "round_trip.distance": gen_distance_m, "round_trip.seed": seed,
            "ch.disable": "true", "elevation": "true", "points_encoded": "false",
            "instructions": "false", "details": "road_class",
        })
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    paths = r.json().get("paths")
    return _make_candidate(seed, gen_distance_m, paths[0]) if paths else None


async def _fire(client, jobs: list[tuple[int, int]], start) -> list[Candidate]:
    """jobs = [(seed, gen_distance_m), ...] fired concurrently from `start`."""
    results = await asyncio.gather(*[_round_trip(client, s, d, start) for s, d in jobs])
    return [c for c in results if c is not None]


# --------------------------------------------------------------------------- generate
async def generate(target_m: float, n: int = 25, tolerance: float = 0.08,
                   k: int = 5, refine_rounds: int = 3,
                   overlap_threshold: float = 0.6, rng_seed: int | None = None,
                   start: tuple = HOME, display_band: float = 0.12) -> dict:
    """Return ranked, de-duplicated top-k candidate loops for target_m meters from `start`.

    display_band (presentation only — NOT scoring): only loops whose distance is within
    this fraction of target are surfaced, so the UI never shows gross padding (e.g. a
    7-mi loop for a 5-mi request). If NOTHING is in-band, fall back to the closest
    achievable so the response is never empty, and the honest shortfall flag still fires.
    """
    target_mi = target_m / MI
    rng = random.Random(rng_seed)
    seeds = rng.sample(range(1, 10_000_000), n)

    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=max(n, 32))) as client:
        pool = await _fire(client, [(s, int(target_m)) for s in seeds], start)

        # Bounded refine: for the best-by-distance seeds not yet in tolerance, re-fire with
        # round_trip.distance nudged by target/actual. Stop early once within tolerance.
        for _ in range(refine_rounds):
            for c in pool:
                score_candidate(c, target_m, target_mi)
            best = min((c for c in pool), key=lambda c: c.breakdown["distance_err"], default=None)
            if best is None or best.breakdown["distance_err"] <= tolerance:
                break
            ranked_by_err = sorted(pool, key=lambda c: c.breakdown["distance_err"])[:max(k + 3, 8)]
            jobs = []
            for c in ranked_by_err:
                if c.distance_m <= 0:
                    continue
                nudged = int(c.gen_distance_m * target_m / c.distance_m)
                nudged = max(500, min(nudged, int(target_m * 2.5)))
                jobs.append((c.seed, nudged))
            if not jobs:
                break
            pool.extend(await _fire(client, jobs, start))

    for c in pool:
        score_candidate(c, target_m, target_mi)
    pool.sort(key=lambda c: c.score)

    def _dedup(cands: list[Candidate]) -> list[Candidate]:
        out: list[Candidate] = []
        for c in cands:
            if all(_jaccard(c.cells, o.cells) <= overlap_threshold for o in out):
                out.append(c)
            if len(out) >= k:
                break
        return out

    # Surface only in-band loops (presentation filter); fall back to closest if none.
    in_band = [c for c in pool if abs(c.distance_m - target_m) / target_m <= display_band]
    kept = _dedup(in_band) if in_band else _dedup(pool)

    best_err = min((c.breakdown["distance_err"] for c in pool), default=1.0)
    shortfall = best_err > tolerance
    return {
        "target_mi": round(target_mi, 2),
        "target_m": round(target_m, 1),
        "tolerance": tolerance,
        "display_band": display_band,
        "n": n,
        "pool_size": len(pool),
        "shortfall": shortfall,
        "message": (f"closest I could get was {kept[0].distance_mi:.2f} mi"
                    if shortfall and kept else None),
        "candidates": [_serialize(c, rank) for rank, c in enumerate(kept, 1)],
    }


def _serialize(c: Candidate, rank: int) -> dict:
    return {
        "rank": rank,
        "seed": c.seed,
        "gen_distance_m": c.gen_distance_m,
        "distance_m": round(c.distance_m, 1),
        "distance_mi": round(c.distance_mi, 2),
        "ascend_m": round(c.ascend_m, 1),
        "descend_m": round(c.descend_m, 1),
        "score": round(c.score, 4),
        "score_breakdown": c.breakdown,
        "road_mix_pct": c.road_mix(),
        "latlngs": [[pt[1], pt[0]] for pt in c.coords],
        "elevations": [round(pt[2], 1) if len(pt) > 2 else None for pt in c.coords],
    }
