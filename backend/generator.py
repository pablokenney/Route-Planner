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

from .segments import segments_on_route

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

# Strava-segment bonus (PLAN.md §Feature 3) — SUBTRACTED from the score, so it pulls
# segment-rich loops up the ranking. Capped at W_SEG (when seg_frac == 1.0). Sized BELOW a
# typical distance error so it breaks ties and nudges among comparable loops but never
# overrides distance accuracy (W_DIST = 1.0 stays dominant); a loop that is 5% closer to
# target still outranks one that merely covers a segment.
W_SEG = 0.10

# Distance-aware expected last-resort fraction (linear 3mi->11mi, then clamped).
_EXP_LO_MI, _EXP_HI_MI = 3.0, 11.0
_EXP_LO_FRAC, _EXP_HI_FRAC = 0.05, 0.25


def expected_lastresort_frac(target_mi: float) -> float:
    t = (target_mi - _EXP_LO_MI) / (_EXP_HI_MI - _EXP_LO_MI)
    t = max(0.0, min(1.0, t))
    return _EXP_LO_FRAC + t * (_EXP_HI_FRAC - _EXP_LO_FRAC)


# --------------------------------------------------------------- surface preference (§Phase 4)
# Soft surface steer as a PER-REQUEST custom model. Legal because the server runs pure flex
# (profiles_ch/profiles_lm empty), where GraphHopper permits multiply_by > 1 per request and
# merges this priority block on TOP of the server-side `run` exclusions — so the no-highway /
# >45 km/h hard-excludes stay absolute regardless of surface choice (PLAN.md §3).
#
# Buckets use GraphHopper's `surface` EncodedValue enum (v10). Anything not named here
# (OTHER, MISSING, COBBLESTONE, WOOD, …) stays NEUTRAL — untagged streets are never demoted,
# mirroring the "never gate residential on a tag" principle of the speed rules.
_SURFACE_PAVED = ("PAVED", "ASPHALT", "CONCRETE", "PAVING_STONES")
_SURFACE_UNPAVED = ("UNPAVED", "COMPACTED", "FINE_GRAVEL", "GRAVEL", "GROUND", "DIRT", "GRASS", "SAND")

# "Gentle nudge" strength (the chosen Phase 4 setting): prefer ×1.4, demote ×0.6. Distance
# accuracy still dominates ranking; surface only reshapes among otherwise-comparable loops.
_SURFACE_PREFER = "1.4"
_SURFACE_DEMOTE = "0.6"

SURFACE_CHOICES = ("any", "paved", "unpaved")


def surface_custom_model(surface: str) -> dict | None:
    """Per-request custom model for a surface preference, or None for 'any' (no preference).

    Returned shape is GraphHopper's flex custom_model: a `priority` list of multiply_by
    statements that GH appends to the profile model. Order within the list is immaterial
    here (the prefer/demote sets are disjoint), and none of these multipliers can un-exclude
    an edge the profile already zeroed.
    """
    surface = (surface or "any").lower()
    if surface == "any":
        return None
    if surface == "paved":
        prefer, demote = _SURFACE_PAVED, _SURFACE_UNPAVED
    elif surface == "unpaved":
        prefer, demote = _SURFACE_UNPAVED, _SURFACE_PAVED
    else:
        raise ValueError(f"unknown surface preference {surface!r} (use one of {SURFACE_CHOICES})")
    priority = [{"if": f"surface == {s}", "multiply_by": _SURFACE_PREFER} for s in prefer]
    priority += [{"if": f"surface == {s}", "multiply_by": _SURFACE_DEMOTE} for s in demote]
    return {"priority": priority}


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
    route_type: str = "loop"          # "loop" | "out_and_back"
    overlap_pct: float = 0.0          # % of the route that is retraced (out-and-back ~100)
    segments: list = field(default_factory=list)  # known Strava segments traversed

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
    c.segments = segments_on_route(coords)
    return c


def _segment_frac(c: Candidate) -> float:
    """Fraction of the route distance covered by known segments (capped at 1.0). Drives the
    W_SEG bonus. Covered meters can exceed distance when segments overlap, so it is clamped."""
    if not c.segments or c.distance_m <= 0:
        return 0.0
    covered = sum(s.get("covered_m", 0.0) for s in c.segments)
    return min(1.0, covered / c.distance_m)


def score_candidate(c: Candidate, target_m: float, target_mi: float) -> None:
    d_err = abs(c.distance_m - target_m) / target_m
    expected = expected_lastresort_frac(target_mi)
    road_pen = max(0.0, c.lastresort_frac() - expected) / (1.0 - expected)
    gain_per_mi = c.ascend_m / max(c.distance_mi, 0.1)
    flat = min(1.0, gain_per_mi / FLAT_CAP_M_PER_MI)
    seg_frac = _segment_frac(c)
    # Subtract the segment bonus: the scorer MINIMIZES, so covering segments lowers the score
    # (better rank). Bounded by W_SEG, well under a typical distance error, so distance wins.
    c.score = W_DIST * d_err + W_ROAD * road_pen + W_FLAT * flat - W_SEG * seg_frac
    c.breakdown = {
        "distance_err": round(d_err, 4),
        "road_penalty": round(road_pen, 4),
        "lastresort_frac": round(c.lastresort_frac(), 4),
        "expected_lastresort": round(expected, 4),
        "flatness": round(flat, 4),
        "segment_frac": round(seg_frac, 4),
        "segment_bonus": round(W_SEG * seg_frac, 4),
    }


# --------------------------------------------------------------------------- GH calls
def round_trip_body(seed: int, gen_distance_m: int, start, custom_model: dict | None,
                    heading: float | None = None, instructions: bool = False) -> dict:
    """POST body for a single round_trip. POST (not GET) is required so a per-request
    `custom_model` (the surface preference) can ride along. Note GH POST uses [lon, lat].

    heading (optional, degrees 0-360 clockwise from north): biases which way the first leg
    leaves `start`. Additive only — default None reproduces the prior body exactly. Phase 5
    milestone mode uses it to steer seeds toward a waypoint bearing and lift filter yield;
    it changes no rule, multiplier, or scoring, just which direction a seed explores.

    instructions: request GraphHopper turn-by-turn. Default False keeps generation/scoring
    cheap and unchanged; only the directions/export path (one chosen loop) turns it on.
    """
    body: dict = {
        "points": [[start[1], start[0]]],
        "profile": "run",
        "algorithm": "round_trip",
        "round_trip.distance": gen_distance_m,
        "round_trip.seed": seed,
        "ch.disable": True,
        "elevation": True,
        "points_encoded": False,
        "instructions": instructions,
        "details": ["road_class"],
    }
    if heading is not None:
        body["headings"] = [round(heading) % 360]
    if custom_model is not None:
        body["custom_model"] = custom_model
    return body


async def _round_trip(client: httpx.AsyncClient, seed: int, gen_distance_m: int, start,
                      custom_model: dict | None = None, heading: float | None = None):
    try:
        r = await client.post(
            f"{GH}/route",
            json=round_trip_body(seed, gen_distance_m, start, custom_model, heading))
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    paths = r.json().get("paths")
    return _make_candidate(seed, gen_distance_m, paths[0]) if paths else None


async def _fire(client, jobs: list[tuple[int, int]], start,
                custom_model: dict | None = None, heading: float | None = None) -> list[Candidate]:
    """jobs = [(seed, gen_distance_m), ...] fired concurrently from `start`."""
    results = await asyncio.gather(
        *[_round_trip(client, s, d, start, custom_model, heading) for s, d in jobs])
    return [c for c in results if c is not None]


# --------------------------------------------------------------------------- generate
async def generate(target_m: float, n: int = 25, tolerance: float = 0.08,
                   k: int = 5, refine_rounds: int = 3,
                   overlap_threshold: float = 0.6, rng_seed: int | None = None,
                   start: tuple = HOME, display_band: float = 0.12,
                   surface: str = "any",
                   extra_candidates: list[Candidate] | None = None) -> dict:
    """Return ranked, de-duplicated top-k candidate loops for target_m meters from `start`.

    display_band (presentation only — NOT scoring): only loops whose distance is within
    this fraction of target are surfaced, so the UI never shows gross padding (e.g. a
    7-mi loop for a 5-mi request). If NOTHING is in-band, fall back to the closest
    achievable so the response is never empty, and the honest shortfall flag still fires.

    surface ('any'|'paved'|'unpaved'): soft per-request preference applied at GENERATION
    time so the candidate set is reshaped by surface (see surface_custom_model). It is NOT
    re-scored here — distance still dominates ranking; surface only changes which loops GH
    proposes. The chosen surface is echoed back so GPX reproduction can rebuild the model.

    extra_candidates (PLAN.md §Feature 2): pre-built Candidates (e.g. out-and-backs from
    backend.outback) folded into the SAME pool, scored by the same objective and competing
    for the same in-band/dedup/top-k slots. They route on profile=run like every loop, so
    the safety model already governs them; here they are just scored and ranked alongside.
    """
    target_mi = target_m / MI
    cmodel = surface_custom_model(surface)
    rng = random.Random(rng_seed)
    seeds = rng.sample(range(1, 10_000_000), n)

    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=max(n, 32))) as client:
        pool = await _fire(client, [(s, int(target_m)) for s in seeds], start, cmodel)

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
            pool.extend(await _fire(client, jobs, start, cmodel))

    if extra_candidates:
        pool.extend(extra_candidates)
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
        "surface": (surface or "any").lower(),
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
        "route_type": c.route_type,
        "overlap_pct": round(c.overlap_pct, 1),
        "segments": c.segments,
        "segment_frac": c.breakdown.get("segment_frac", 0.0),
        "latlngs": [[pt[1], pt[0]] for pt in c.coords],
        "elevations": [round(pt[2], 1) if len(pt) > 2 else None for pt in c.coords],
    }


def parse_instructions(path: dict) -> list[dict]:
    """Turn a GraphHopper `instructions` array into a compact, UI/export-ready turn list.

    Each GH instruction carries `text`, `distance` (m to the NEXT instruction), `time`,
    `sign` (turn code -3..6), and `interval` ([from, to] coordinate indices). We emit the
    cumulative distance AT each turn so course points can be placed unambiguously on loops
    and out-and-backs (where the same lat/lon recurs). The terminal "arrive" instruction
    (sign 4) is kept — it marks the finish.
    """
    out: list[dict] = []
    cumulative = 0.0
    for ins in path.get("instructions", []) or []:
        interval = ins.get("interval", [None, None])
        out.append({
            "text": ins.get("text", ""),
            "sign": int(ins.get("sign", 0)),
            "distance_m": round(float(ins.get("distance", 0.0)), 1),
            "cumulative_m": round(cumulative, 1),
            "coord_index": interval[0] if interval else None,
        })
        cumulative += float(ins.get("distance", 0.0))
    return out


def _bearing(p, q) -> float:
    """Initial great-circle bearing (deg, 0=N clockwise) from p to q; both [lon, lat]."""
    lo1, la1, lo2, la2 = map(math.radians, [p[0], p[1], q[0], q[1]])
    dlo = lo2 - lo1
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def synthesize_turns(coords: list, turn_threshold_deg: float = 35.0,
                     min_gap_m: float = 30.0) -> list[dict]:
    """Derive a turn list from raw geometry when GraphHopper instructions aren't available
    (out-and-back and milestone COMPOSITES have no reproducing round_trip seed). Mirrors a
    watch app's "bend detection": where the heading swings more than turn_threshold_deg
    between consecutive look-ahead segments, emit a turn. coords are [lon, lat, (ele)].

    Returns the SAME shape as parse_instructions (text/sign/distance_m/cumulative_m/
    coord_index) so the UI and TCX/GPX builders treat both sources identically. Signs follow
    GraphHopper's convention: -2 left, -1 slight-left, 0 continue, 1 slight-right, 2 right,
    4 arrive — enough for course-point PointType mapping.
    """
    if len(coords) < 2:
        return []
    # Segment lengths and bearings along the polyline.
    seg_len = [_hav(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    seg_brg = [_bearing(coords[i], coords[i + 1]) for i in range(len(coords) - 1)]
    cum = [0.0]
    for d in seg_len:
        cum.append(cum[-1] + d)

    turns = [{"text": "Head out", "sign": 0, "distance_m": 0.0,
              "cumulative_m": 0.0, "coord_index": 0}]
    last_turn_cum = 0.0
    for i in range(1, len(seg_brg)):
        # Skip near-zero-length segments (duplicate vertices at merge seams).
        if seg_len[i] < 1.0:
            continue
        delta = ((seg_brg[i] - seg_brg[i - 1] + 180.0) % 360.0) - 180.0  # signed [-180,180]
        if abs(delta) < turn_threshold_deg:
            continue
        if cum[i] - last_turn_cum < min_gap_m:  # debounce closely-spaced bends
            continue
        right = delta > 0
        sharp = abs(delta) >= 75.0
        if sharp:
            sign, word = (2, "Turn right") if right else (-2, "Turn left")
        else:
            sign, word = (1, "Slight right") if right else (-1, "Slight left")
        turns.append({"text": word, "sign": sign,
                      "distance_m": round(cum[i] - last_turn_cum, 1),
                      "cumulative_m": round(cum[i], 1), "coord_index": i})
        last_turn_cum = cum[i]
    # Update the prior turn's distance-to-next as we go, then append arrival.
    for j in range(len(turns) - 1):
        turns[j]["distance_m"] = round(turns[j + 1]["cumulative_m"] - turns[j]["cumulative_m"], 1)
    turns.append({"text": "Arrive at finish", "sign": 4, "distance_m": 0.0,
                  "cumulative_m": round(cum[-1], 1), "coord_index": len(coords) - 1})
    return turns
