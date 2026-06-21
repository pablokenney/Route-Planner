"""Auto-seek known Strava segments near the start and build loops THROUGH them, to fold into
the Loops-tab candidate pool (PLAN.md §Feature 3 follow-up).

Why this exists: GraphHopper's round_trip loop generator is stochastic and direction-seeded —
it never targets a specific trail, so a loved local segment (e.g. the LeTort Spring Run and
Nature Trail) almost never appears on its own, and a heading hint toward it frequently fails
to close a loop at all ("Connection not found"). The existing W_SEG segment bonus can only
RE-RANK loops that already cover a segment; it can't CREATE one.

So here we take each known segment within reach of the start and CONSTRUCT loops that run it
end-to-end: a round_trip PAD loop at the start sized to the remaining budget, then an
out-and-back that runs the segment (start→nearEnd→[segment]→farEnd and retrace). Routing
through BOTH segment endpoints forces traversal of the whole segment (not a glancing touch);
padding at the START — not the segment's far end, which is often a poorly-connected creek-side
dead-end where round_trips fail or scatter — keeps construction reliable; and the pad is REFINED
toward target so the composite lands in-band. The results fold in as `extra_candidates` —
generate() scores, de-dups, and ranks them by the same objective, and W_SEG floats them up.
Every leg is profile=run, so the safety model + road block still apply.
"""
from __future__ import annotations

import asyncio
import random

import httpx

from .generator import GH, Candidate, _hav, _make_candidate, round_trip_body, route_custom_model
from .milestone import MIN_PAD_M, _merge_paths, _reverse_path, _route
from .segments import _SEGMENTS

# Nearest reachable segments to seek per request. Each costs one route + a small refined
# fan-out, so this bounds the added latency (results are cached by /api/routes anyway).
MAX_SEGMENTS = 5
# Two segments whose near endpoints are within this distance are treated as the same place
# (e.g. the two LeTort trail segments) so we don't build redundant loops for one trail.
DEDUP_M = 200.0
# Refined pad-loop fan-out: seeds fired per segment and how many times we nudge them toward
# the remaining budget (round_trip is inaccurate single-shot — refine is what makes the
# composite land in-band reliably, mirroring generator.generate's own refine).
PAD_SEEDS = 8
PAD_REFINE_ROUNDS = 3
# Best composites kept per segment. Padding at the start yields many in-band loops; keeping just
# the most distance-accurate few per trail avoids flooding the pool with near-identical loops so
# several different segments (and plain random loops) can share the displayed results.
PER_SEGMENT = 2


async def segment_loops(start: tuple, target_m: float, surface: str = "any",
                        tolerance: float = 0.08, k: int = 5,
                        band: float = 0.12) -> list[Candidate]:
    """Build loops that run the nearest known segments end-to-end and fit a target_m loop.

    Returns UNSCORED Candidates (route_type='loop', each covering a segment) ready for
    generator.generate(extra_candidates=...). Empty when no segment is in range or the segment
    cache is absent — so the Loops tab degrades to its prior behavior. Only loops within `band`
    of target are returned (off-distance composites would never display).
    """
    if not _SEGMENTS:
        return []
    start_ll = [start[1], start[0]]
    cmodel = route_custom_model(surface)

    # Rank segments by their point nearest the start (a long trail close by would rank far if
    # measured at its midpoint), pair each with its two endpoints to force end-to-end traversal,
    # keep only those a round-trip can plausibly fit, then drop near-duplicate trails.
    reachable = []
    for s in _SEGMENTS:
        pts = s["points"]
        if len(pts) < 2:
            continue
        crow = min(_hav(start_ll, p) for p in pts)
        if 2 * crow > target_m * (1 + tolerance):
            continue
        e0, e1 = pts[0], pts[-1]
        near, far = (e0, e1) if _hav(start_ll, e0) <= _hav(start_ll, e1) else (e1, e0)
        reachable.append((crow, near, far))
    reachable.sort(key=lambda x: x[0])

    chosen: list = []
    for crow, near, far in reachable:
        if any(_hav(near, n) <= DEDUP_M for _, n, _ in chosen):
            continue
        chosen.append((crow, near, far))
        if len(chosen) >= MAX_SEGMENTS:
            break
    if not chosen:
        return []

    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=32)) as client:
        built = await asyncio.gather(
            *[_loops_through_via(client, start_ll, [near, far], target_m, cmodel, band, k)
              for _, near, far in chosen])

    out: list[Candidate] = []
    for group in built:
        out.extend(group)
    return out


# Curated dead-end trail tips worth running an out-and-back to. A loop never reaches these on
# its own (they dead-end — e.g. the LeTort Nature Trail's south end only connects onward via
# Heisers Lane, which the safety block excludes), so we construct a down-and-back spur to each
# one in range and fold it in. Because the tip is reachable ONLY via the trail, the out leg
# necessarily runs the trail down and the back leg retraces it. Each entry is {name, lat, lon}.
SPUR_TIPS = [
    {"name": "LeTort Nature Trail (south end)", "lat": 40.16197, "lon": -77.17292},
]


async def spur_loops(start: tuple, target_m: float, surface: str = "any",
                     tolerance: float = 0.08, k: int = 5, band: float = 0.12) -> list[Candidate]:
    """Loops that include a down-and-back to a curated dead-end trail tip (SPUR_TIPS), folded into
    the loop pool like segment_loops. Marked as retraces. Empty when no tip is in range."""
    if not SPUR_TIPS:
        return []
    start_ll = [start[1], start[0]]
    cmodel = route_custom_model(surface)
    tips = [[t["lon"], t["lat"]] for t in SPUR_TIPS
            if 2 * _hav(start_ll, [t["lon"], t["lat"]]) <= target_m * (1 + tolerance)]
    if not tips:
        return []
    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=32)) as client:
        built = await asyncio.gather(
            *[_loops_through_via(client, start_ll, [tip], target_m, cmodel, band, k, spur=True)
              for tip in tips])
    out: list[Candidate] = []
    for group in built:
        out.extend(group)
    return out


async def _loops_through_via(client: httpx.AsyncClient, start_ll, via: list,
                             target_m: float, cmodel: dict | None, band: float,
                             k: int, spur: bool = False) -> list[Candidate]:
    """[pad loop @ start]→[start→via…]→[retrace] composites sized to target_m. The out leg routes
    through the via points (a segment's two endpoints, or a single dead-end trail tip) and the
    back leg retraces it, so the out-and-back runs that trail; the pad round_trip is at the START
    (reliable) and is REFINED toward the remaining budget so composites land in-band.

    spur=True marks results as retraces (route_type='out_and_back' + overlap_pct) — used for
    dead-end tips, where the out-and-back is most of the route. Returns only in-band Candidates."""
    out = await _route(client, [start_ll, *via], cmodel)  # start→via… (and retrace home)
    if out is None:
        return []
    back = _reverse_path(out)
    d_out = float(out.get("distance", 0.0))
    pad_budget = target_m - 2.0 * d_out

    def in_band(c: Candidate) -> bool:
        return abs(c.distance_m - target_m) / target_m <= band

    def tag(c: Candidate) -> Candidate:
        if spur:  # the down-and-back trail leg is retraced — label honestly so the UI badges it
            c.route_type = "out_and_back"
            c.overlap_pct = round(100.0 * min(1.0, 2.0 * d_out / max(c.distance_m, 1.0)), 1)
        return c

    # The out-and-back alone already ≈ target (no room to pad): take it if it fits.
    if pad_budget < MIN_PAD_M:
        c = _make_candidate(seed=-1, gen_distance_m=int(round(target_m)),
                            path=_merge_paths([out, back]))
        return [tag(c)] if in_band(c) else []

    start_pt = (start_ll[1], start_ll[0])  # round_trip_body wants (lat, lon)

    async def _round_trip(seed: int, gen: float) -> dict | None:
        try:
            r = await client.post(f"{GH}/route",
                                  json=round_trip_body(seed, int(gen), start_pt, cmodel))
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        ps = r.json().get("paths")
        return ps[0] if ps else None

    rng = random.Random()
    jobs = [(s, pad_budget) for s in rng.sample(range(1, 10_000_000), PAD_SEEDS)]
    kept: list[Candidate] = []
    for _ in range(PAD_REFINE_ROUNDS):
        paths = await asyncio.gather(*[_round_trip(s, g) for s, g in jobs])
        retry: list = []
        for (s, g), p in zip(jobs, paths):
            if p is None:
                continue
            # pad loop returns to start, then the out-and-back runs the trail and comes home.
            c = _make_candidate(seed=0, gen_distance_m=int(round(target_m)),
                                path=_merge_paths([p, out, back]))
            if in_band(c):
                kept.append(tag(c))
                continue
            # Nudge this seed's pad target toward the budget the actual loop missed.
            pad_actual = float(p.get("distance", 0.0))
            if pad_actual > 0:
                ng = max(MIN_PAD_M, min(g * pad_budget / pad_actual, target_m * 2.5))
                retry.append((s, ng))
        if len(kept) >= k or not retry:
            break
        jobs = retry
    # Keep only the most distance-accurate few (avoid flooding with near-duplicates).
    kept.sort(key=lambda c: abs(c.distance_m - target_m))
    return kept[:PER_SEGMENT]
