"""Out-and-back routes (PLAN.md §Feature 2).

A pure out-and-back goes from the start to a turnaround ~half the target away and retraces
the same path home — a fully acceptable route, explicitly flagged as retraced so the UI can
say so. These are generated here, then folded into the SAME candidate pool as loops via
``generator.generate(extra_candidates=...)``, scored by the same distance-dominant objective,
and ranked together.

Built strictly ON TOP of the locked engine, exactly like milestone mode: the outbound leg is
a plain shortest path on ``profile=run`` (so the no-highway / ≤25 mph safety model governs it),
the return leg is that path reversed (``milestone._reverse_path``), and the two are stitched
with ``milestone._merge_paths``. No rule, multiplier, or scoring weight is touched here.

Turnaround selection: project a crow-flies destination along each of several bearings at a
fraction of half-target, route to it, and bounded-refine the fraction toward the road distance
that makes the round trip hit the target (roads wander, so the crow-flies distance is shorter
than the road distance — the same target/actual nudge the core generator uses for round_trip).
"""
from __future__ import annotations

import asyncio
import math

import httpx

from .generator import (
    GH,
    HOME,
    MI,
    Candidate,
    _cells,
    _jaccard,
    _make_candidate,
    project_point,
    route_custom_model,
)
from .milestone import _merge_paths, _reverse_path, _route

# Bearings sampled around the start (evenly spaced). Each yields at most one out-and-back.
N_BEARINGS = 8
# Bounded refine of the crow-flies projection factor toward the road distance that hits target.
REFINE_ROUNDS = 4
# Default acceptance band for an out-and-back's total distance (wider than core tol — the
# turnaround geometry is coarser than round_trip's; still well inside the display band).
TOLERANCE = 0.12
# Max distinct out-and-backs returned (they compete with loops for the final top-k slots).
MAX_RETURN = 4
# Initial guess: roads wander, so the crow-flies turnaround sits inside half the road distance.
_INIT_FACTOR = 0.75


async def _one(client: httpx.AsyncClient, start_ll, bearing: float, target_m: float,
               cmodel: dict | None, tolerance: float) -> Candidate | None:
    """Build a single out-and-back toward `bearing`, refining the turnaround toward target.

    Returns None when this bearing can't be brought into tolerance — e.g. it points into
    excluded terrain (a mountain, a highway corridor) so the only road there is a huge
    detour. Dropping the bearing keeps every returned out-and-back honestly near-distance,
    rather than surfacing a 21-mi route for a 5-mi request.
    """
    half = target_m / 2.0
    factor = _INIT_FACTOR
    best = None  # (abs distance error fraction, leg) — keep the closest attempt seen
    for _ in range(REFINE_ROUNDS):
        dest = project_point(start_ll, bearing, half * factor)
        leg = await _route(client, [start_ll, dest], cmodel)
        if leg is None:
            return None
        leg_m = float(leg.get("distance", 0.0))
        if leg_m <= 0:
            return None
        err = abs(2 * leg_m - target_m) / target_m
        if best is None or err < best[0]:
            best = (err, leg)
        if err <= tolerance:
            break
        # Nudge the crow-flies guess toward the road distance that would hit half-target.
        factor *= half / leg_m
        factor = max(0.2, min(factor, 1.6))

    # Only accept a bearing whose best attempt is within the band; else this direction has no
    # reachable turnaround at the right distance, so drop it.
    if best is None or best[0] > tolerance:
        return None
    leg = best[1]

    back = _reverse_path(leg)
    merged = _merge_paths([leg, back])
    # seed encodes the bearing for traceability; out-and-backs are reproduced from geometry
    # (the /api/*_track endpoints), never by round_trip seed, so this is just an identifier.
    c = _make_candidate(seed=1_000_000 + int(round(bearing)), gen_distance_m=int(target_m),
                        path=merged)
    c.route_type = "out_and_back"
    # Honest retrace figure: cell-overlap of the two halves (≈100% for an exact retrace).
    c.overlap_pct = 100.0 * _jaccard(_cells(leg["points"]["coordinates"]),
                                     _cells(back["points"]["coordinates"]))
    return c


def _dedup(cands: list[Candidate], k: int, overlap_threshold: float = 0.6) -> list[Candidate]:
    """Keep up to k out-and-backs that are spatially distinct from one another (different
    directions out of the start). Geometry-only — scoring happens later in generate()."""
    out: list[Candidate] = []
    for c in cands:
        if all(_jaccard(c.cells, o.cells) <= overlap_threshold for o in out):
            out.append(c)
        if len(out) >= k:
            break
    return out


async def out_and_backs(start: tuple = HOME, target_m: float = 5 * MI, surface: str = "any",
                        n_bearings: int = N_BEARINGS, tolerance: float = TOLERANCE,
                        k: int = MAX_RETURN) -> list[Candidate]:
    """Generate up to k distinct out-and-back Candidates from `start` at ~target_m total.

    Returns UNSCORED Candidates (route_type='out_and_back', overlap_pct set) ready to hand to
    generator.generate(extra_candidates=...), where they are scored and ranked beside loops.
    """
    start_ll = [start[1], start[0]]
    cmodel = route_custom_model(surface)
    bearings = [i * 360.0 / n_bearings for i in range(n_bearings)]
    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=max(n_bearings, 16))) as client:
        built = await asyncio.gather(
            *[_one(client, start_ll, b, target_m, cmodel, tolerance) for b in bearings])
    return _dedup([c for c in built if c is not None], k)
