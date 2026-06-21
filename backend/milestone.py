"""Phase 5 — Milestone Mode: exact waypoints, padded distance (PLAN.md §Phase 5).

"Anchor what I know, generate what I don't." Given a start, one or more required
waypoints, and a target distance, produce loops that pass through EVERY waypoint and pad
out to the target (±5–8%).

This module sits strictly ON TOP of the locked Phase 0–2 engine. It imports the generator's
own primitives (`_fire`, `_make_candidate`, `score_candidate`, `_serialize`, `_cells`,
`_jaccard`, `route_custom_model`) and never re-implements or relaxes any of them. Every
GraphHopper call here uses `profile=run`, so the full safety model (no motorway/trunk/
primary/secondary, no explicit max_speed>45, the tertiary penalties, the footway/path
preferences) applies to EVERY leg automatically. A waypoint is snapped by GraphHopper to the
nearest *runnable* edge — it can never pull a route onto an excluded road.

Two mechanisms, chosen by yield (PLAN.md §Phase 5):
  • FILTER (primary): fire N round_trips from start at the target distance (the unchanged
    Phase 2 fan-out, biased with a `headings` hint toward the first waypoint to lift yield),
    keep only loops whose geometry passes within ε of every waypoint. Inherits the proven
    accuracy, de-dup, and seed variety for free. Preferred when it yields enough loops.
  • DECOMPOSE (fallback): GUARANTEE inclusion by construction — spine start→wp₁→…→wp_L, a
    round_trip loop anchored at the last waypoint sized to the remaining budget, then the
    spine back. Always hits every waypoint; costs a specific out-loop-back shape. Variety
    comes from varying the pad-loop seed (round_trip cannot combine with GraphHopper's
    alternative_route algorithm, so seed variation is the faithful analog of the core
    generator's own variety mechanism).

The one hard failure: D_spine (the shortest loop that visits every waypoint and returns)
already exceeds the target. You can pad UP but not DOWN, so this is unsatisfiable — reported
honestly, never by silently dropping a waypoint.
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
    _fire,
    _jaccard,
    _make_candidate,
    _serialize,
    project_point,
    route_custom_model,
    score_candidate,
)

# --- tunables (PLAN.md §Phase 5: "Choose/document ε" and "Document the threshold") --------
# ε — how close a candidate's polyline must pass to a snapped waypoint to count as "through"
# it. The waypoint is already snapped onto a runnable edge; a loop that traverses that edge
# passes within a few metres, so 40 m comfortably admits true passes while rejecting loops
# that merely pass nearby on a parallel street. Measured against the Dickinson anchor.
EPS_M = 40.0

# Trigger — minimum DISTINCT in-tolerance filtered loops required to PREFER the filter path.
# Below this we fall back to decomposition for a guaranteed-correct result. 3 gives the
# candidate switcher real choice; raise it to favour decomposition, lower to favour filtered.
FILTER_MIN_CANDIDATES = 3

# Filter fan-out size. Larger than the core default (25) because waypoint filtering discards
# most seeds — a near-home anchor still leaves plenty, a constrained one trips the fallback.
FILTER_N = 40

# Pad-loop seeds tried in decomposition (deduped down to the distinct ones we return).
DECOMP_PAD_SEEDS = 12

# Below this remaining budget (m) the spine out-and-back already ≈ target, so we skip the
# pad loop and return the spine itself rather than bolt on a pointless tiny loop.
MIN_PAD_M = 400.0


# ----------------------------------------------------------------------------- geometry
# All geometry here works in GraphHopper coordinate order: [lon, lat, (ele)].
def _initial_bearing(a, b) -> float:
    """Initial great-circle bearing (deg, 0=N clockwise) from point a to b; both [lon,lat]."""
    lo1, la1, lo2, la2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlo = lo2 - lo1
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _hav_m(a, b) -> float:
    """Haversine metres between two [lon,lat] points."""
    R = 6371000.0
    lo1, la1, lo2, la2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _pt_seg_dist_m(p, a, b) -> float:
    """Distance (m) from point p to segment a-b, all [lon,lat]. Local equirectangular
    projection at p's latitude — accurate at the few-hundred-metre scale of a street edge.
    """
    lat0 = math.radians(p[1])
    mlon = 111320.0 * math.cos(lat0)  # m per degree lon at this latitude
    mlat = 110540.0                   # m per degree lat
    px, py = p[0] * mlon, p[1] * mlat
    ax, ay = a[0] * mlon, a[1] * mlat
    bx, by = b[0] * mlon, b[1] * mlat
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def min_dist_to_polyline_m(pt, coords) -> float:
    """Min distance (m) from pt to a polyline. pt and coords entries are [lon,lat,(ele)].
    Point-to-segment (not just vertex) so sparse straight edges aren't false-negatives.
    """
    if len(coords) < 2:
        return _hav_m(pt, coords[0]) if coords else float("inf")
    return min(_pt_seg_dist_m(pt, coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def passes_through(coords, waypoints, eps_m: float = EPS_M) -> bool:
    """True iff the polyline passes within eps_m of EVERY waypoint. waypoints are [lon,lat]."""
    return all(min_dist_to_polyline_m(w, coords) <= eps_m for w in waypoints)


# ---------------------------------------------------------------------- GH route helpers
def _route_body(points_lonlat: list, custom_model: dict | None, algorithm: str | None = None) -> dict:
    """POST body for a plain (non-round_trip) route through ordered via-points. profile=run
    so the safety model governs every leg; flex (ch.disable) so the custom_model is honoured.
    """
    body: dict = {
        "points": points_lonlat,
        "profile": "run",
        "ch.disable": True,
        "elevation": True,
        "points_encoded": False,
        "instructions": False,
        "details": ["road_class"],
    }
    if algorithm:
        body["algorithm"] = algorithm
    if custom_model is not None:
        body["custom_model"] = custom_model
    return body


async def _route(client: httpx.AsyncClient, points_lonlat: list,
                 custom_model: dict | None) -> dict | None:
    """Single shortest path through ordered via-points. Returns the raw GH path dict
    (so callers can read snapped_waypoints) or None on failure."""
    try:
        r = await client.post(f"{GH}/route", json=_route_body(points_lonlat, custom_model))
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    paths = r.json().get("paths")
    return paths[0] if paths else None


def _merge_paths(paths: list[dict]) -> dict:
    """Concatenate GH path dicts into one synthetic path so _make_candidate can build a
    Candidate from a composite (decomposition) route. Distances/ascent come from the sub-
    paths' own totals (not recomputed), so the duplicate vertex at each seam is harmless.
    road_class detail ranges are re-based by the running coordinate offset.
    """
    coords: list = []
    rc: list = []
    dist = asc = desc = 0.0
    for p in paths:
        off = len(coords)
        pc = p["points"]["coordinates"]
        coords.extend(pc)
        for fr, to, cls in p.get("details", {}).get("road_class", []):
            rc.append([fr + off, to + off, cls])
        dist += float(p.get("distance", 0.0))
        asc += float(p.get("ascend", 0.0))
        desc += float(p.get("descend", 0.0))
    return {"points": {"coordinates": coords}, "distance": dist,
            "ascend": asc, "descend": desc, "details": {"road_class": rc}}


def _reverse_path(p: dict) -> dict:
    """Reverse a path's geometry (for the return leg of the spine). Coordinates reverse;
    ascend/descend swap; road_class ranges are mirrored to the reversed index space."""
    coords = list(reversed(p["points"]["coordinates"]))
    n = len(coords)
    rc = [[n - 1 - to, n - 1 - fr, cls]
          for fr, to, cls in p.get("details", {}).get("road_class", [])]
    rc.sort()
    return {"points": {"coordinates": coords}, "distance": float(p.get("distance", 0.0)),
            "ascend": float(p.get("descend", 0.0)), "descend": float(p.get("ascend", 0.0)),
            "details": {"road_class": rc}}


# --------------------------------------------------------------------------- de-dup / topk
def _dedup_topk(cands: list[Candidate], k: int, overlap_threshold: float = 0.6) -> list[Candidate]:
    out: list[Candidate] = []
    for c in sorted(cands, key=lambda c: c.score):
        if all(_jaccard(c.cells, o.cells) <= overlap_threshold for o in out):
            out.append(c)
        if len(out) >= k:
            break
    return out


# ------------------------------------------------------------------- out-and-back through wps
# Bearings tried when extending PAST the last waypoint to pad an out-and-back to target.
OB_BEARINGS = 8
OB_REFINE = 4


async def _out_and_back(client: httpx.AsyncClient, start_ll, wp_ll, snapped_wp_ll,
                        target_m: float | None, cmodel: dict | None, tolerance: float,
                        eps_m: float, k: int) -> list[Candidate]:
    """Build out-and-back routes that go OUT through every waypoint (in order) and retrace the
    EXACT same path home. A true retrace — distinct from the loops the filter/decompose paths
    return — so a runner can hit known points and come straight back.

    target_m is None (derived) → the plain there-and-back: route start→wp₁→…→wp_L, reverse it.
    target_m set (pad)         → if the plain retrace is short of target, EXTEND past the last
                                 waypoint to a turnaround sized so the round trip ≈ target, then
                                 retrace from there. Several bearings give distinct options.

    Every leg routes on profile=run, so the safety model governs the whole route; each result
    is re-checked to pass within ε of every waypoint (the out leg guarantees it by construction).
    """
    out = await _route(client, [start_ll, *wp_ll], cmodel)  # one-way spine through the wps
    if out is None:
        return []
    d_out = float(out.get("distance", 0.0))

    # Derived, or padding budget too small to bother extending: the plain there-and-back.
    plain_extra = (target_m is not None) and (target_m - 2 * d_out >= MIN_PAD_M)
    cands: list[Candidate] = []
    if target_m is None or not plain_extra:
        merged = _merge_paths([out, _reverse_path(out)])
        c = _make_candidate(seed=0, gen_distance_m=int(round(2 * d_out)), path=merged)
        if passes_through(c.coords, snapped_wp_ll, eps_m):
            _tag_out_and_back(c, out, _reverse_path(out))
            cands.append(c)
        return cands

    # Pad: extend past the last waypoint along several bearings to a turnaround, retrace whole.
    anchor = snapped_wp_ll[-1]
    half_extra = (target_m - 2 * d_out) / 2.0  # one-way extra distance beyond the last wp
    bearings = [i * 360.0 / OB_BEARINGS for i in range(OB_BEARINGS)]

    async def _leg(bearing: float):
        factor = 0.75
        best = None
        for _ in range(OB_REFINE):
            dest = project_point(anchor, bearing, max(half_extra, 1.0) * factor)
            ext = await _route(client, [anchor, dest], cmodel)
            if ext is None:
                return None
            ext_m = float(ext.get("distance", 0.0))
            if ext_m <= 0:
                return None
            total = 2 * (d_out + ext_m)
            err = abs(total - target_m) / target_m
            if best is None or err < best[0]:
                best = (err, ext)
            if err <= tolerance:
                break
            factor *= max(half_extra, 1.0) / ext_m
            factor = max(0.2, min(factor, 1.6))
        if best is None or best[0] > max(tolerance, 0.12):
            return None
        ext = best[1]
        # Full out path = spine to last wp, then the extension; retrace the whole thing.
        out_full = _merge_paths([out, ext])
        back_full = _reverse_path(out_full)
        merged = _merge_paths([out_full, back_full])
        c = _make_candidate(seed=2_000_000 + int(round(bearing)),
                            gen_distance_m=int(round(target_m)), path=merged)
        if not passes_through(c.coords, snapped_wp_ll, eps_m):
            return None
        _tag_out_and_back(c, out_full, back_full)
        return c

    built = await asyncio.gather(*[_leg(b) for b in bearings])
    return _dedup_topk([c for c in built if c is not None], k)


def _tag_out_and_back(c: Candidate, out_path: dict, back_path: dict) -> None:
    """Flag a candidate as a retraced out-and-back and record its retrace overlap (≈100%)."""
    c.route_type = "out_and_back"
    c.overlap_pct = 100.0 * _jaccard(_cells(out_path["points"]["coordinates"]),
                                     _cells(back_path["points"]["coordinates"]))


# --------------------------------------------------------------------------- orchestrator
async def milestone(start: tuple, waypoints: list[tuple], target_m: float | None,
                    surface: str = "any", pad: bool = True, eps_m: float = EPS_M,
                    n: int = FILTER_N, tolerance: float = 0.08, k: int = 5,
                    filter_min: int = FILTER_MIN_CANDIDATES) -> dict:
    """Generate routes through every waypoint, padded to target_m (or derived if pad=False).

    start, waypoints: (lat, lon) tuples (UI/geocoder order). Internally we work in [lon,lat].
    pad=False  → "exact-waypoints, derived-distance": route through the points and report
                 whatever distance results (target_m ignored). Same machinery, no padding.
    pad=True   → pad UP to target_m within tolerance via FILTER, else DECOMPOSE.

    Pure OUT-AND-BACK routes (run out through every waypoint and retrace the same path home)
    are ALSO built and folded into the same candidate pool — scored, de-duplicated, and ranked
    alongside the loops, exactly as the Loops tab folds out-and-backs via `extra_candidates`.
    Each carries route_type='out_and_back' so the UI badges it.

    Returns the core candidate shape plus milestone metadata: snapped waypoints (with a
    moved flag so the UI can explain snapping), the method that fired, D_spine, and the
    honest over_spine flag when the waypoints are already farther apart than the target.
    """
    # GH wants [lon, lat]; the UI speaks (lat, lon).
    start_ll = [start[1], start[0]]
    wp_ll = [[w[1], w[0]] for w in waypoints]
    cmodel = route_custom_model(surface)

    async with httpx.AsyncClient(timeout=60.0,
                                 limits=httpx.Limits(max_connections=max(n, 32))) as client:
        # --- preflight: the spine start→wp₁→…→wp_L→start. Gives snapped waypoints, D_spine
        # (the genuine minimum loop visiting all waypoints), AND it IS the pad=False answer.
        spine_pts = [start_ll, *wp_ll, start_ll]
        spine = await _route(client, spine_pts, cmodel)
        if spine is None:
            return {"error": "Could not route through those waypoints (is GraphHopper up?)",
                    "candidates": []}

        snapped = spine.get("snapped_waypoints", {}).get("coordinates", [])
        # snapped_waypoints align with the request points [start, *wps, start]; the waypoint
        # rows are indices 1..L. Report each with how far GH moved it onto a runnable edge.
        snapped_wps = []
        for i, w in enumerate(wp_ll):
            s = snapped[i + 1] if i + 1 < len(snapped) else w
            moved = _hav_m(w, s)
            snapped_wps.append({
                "requested": [w[1], w[0]], "snapped": [s[1], s[0]],
                "moved_m": round(moved, 1), "moved": moved > 5.0,
            })
        snapped_wp_ll = [[sw["snapped"][1], sw["snapped"][0]] for sw in snapped_wps]
        d_spine = float(spine.get("distance", 0.0))

        meta = {
            "start": [start[0], start[1]],
            "waypoints": [[w[1], w[0]] for w in wp_ll],
            "snapped_waypoints": snapped_wps,
            "d_spine_m": round(d_spine, 1),
            "d_spine_mi": round(d_spine / MI, 2),
            "surface": (surface or "any").lower(),
            "eps_m": eps_m,
            "filter_threshold": filter_min,
        }

        # --- pad=False: derived distance — the spine through the waypoints, as-is. (Folding an
        # out-and-back here is moot: with no target to pad to we score against d_spine, which
        # the spine itself fits exactly, so it always wins. Out-and-backs fold into pad mode.)
        if not pad or target_m is None:
            c = _make_candidate(seed=0, gen_distance_m=int(round(d_spine)), path=spine)
            score_candidate(c, d_spine or 1.0, (d_spine or 1.0) / MI)
            return {**meta, "method": "derived", "pad": False, "target_mi": None,
                    "over_spine": False, "shortfall": False, "message": None,
                    "candidates": [_serialize(c, 1)]}

        target_mi = target_m / MI
        meta["target_mi"] = round(target_mi, 2)
        meta["target_m"] = round(target_m, 1)
        meta["tolerance"] = tolerance

        # --- the one hard failure: spine already longer than target → cannot pad down.
        if d_spine > target_m * (1 + tolerance):
            return {**meta, "method": "over_spine", "pad": True, "over_spine": True,
                    "shortfall": True, "candidates": [],
                    "message": (f"the points you picked already make ~{d_spine / MI:.1f} mi; "
                                f"can't build a {target_mi:.1f}-mi loop through all of them")}

        # --- pure out-and-backs through the waypoints, padded to target. Folded into whichever
        # loop pool is chosen below and ranked alongside the loops (mirrors the Loops tab's
        # extra_candidates). Built once here; each is already tagged route_type='out_and_back'.
        ob_cands = await _out_and_back(client, start_ll, wp_ll, snapped_wp_ll, target_m,
                                       cmodel, tolerance, eps_m, k)
        for c in ob_cands:
            score_candidate(c, target_m, target_mi)

        # --- FILTER (primary): fire the unchanged round_trip fan-out at the target, biased
        # toward the first waypoint, and keep loops that pass within ε of EVERY waypoint.
        rng = _seeded_seeds(n)
        heading = _initial_bearing(start_ll, snapped_wp_ll[0])
        fired = await _fire(client, [(s, int(target_m)) for s in rng], (start[0], start[1]),
                            cmodel, heading)
        for c in fired:
            score_candidate(c, target_m, target_mi)
        kept = [c for c in fired
                if abs(c.distance_m - target_m) / target_m <= max(tolerance, 0.12)
                and passes_through(c.coords, snapped_wp_ll, eps_m)]
        # The yield decision (filter vs decompose) is about LOOP quality, so it counts distinct
        # loops only — out-and-backs are an addition, not a substitute for real filtered loops.
        filtered_loops = _dedup_topk(kept, k)

        if len(filtered_loops) >= filter_min:
            chosen = _dedup_topk(kept + ob_cands, k)
            best_err = min((c.breakdown["distance_err"] for c in chosen), default=1.0)
            return {**meta, "method": "filter", "pad": True, "over_spine": False,
                    "filter_yield": len(filtered_loops),
                    "shortfall": best_err > tolerance,
                    "message": (f"closest route was {chosen[0].distance_mi:.2f} mi"
                                if best_err > tolerance else None),
                    "candidates": [_serialize(c, r) for r, c in enumerate(chosen, 1)]}

        # --- DECOMPOSE (fallback): guarantee inclusion by construction.
        decomposed = await _decompose(client, start_ll, wp_ll, snapped_wp_ll, target_m,
                                      cmodel, k, tolerance)
        for c in decomposed:
            score_candidate(c, target_m, target_mi)
        # Prefer decomposition (guaranteed inclusion by construction); if it somehow returned
        # nothing, fall back to whatever in-ε filtered loops we did find. Out-and-backs fold in
        # either way, so a pure-out-and-back result is still possible when no loop survives.
        loop_pool = decomposed if decomposed else kept
        method = "decompose" if decomposed else ("filter" if kept else "out_and_back")
        chosen = _dedup_topk(loop_pool + ob_cands, k)
        if not chosen:
            return {**meta, "method": "none", "pad": True, "over_spine": False,
                    "shortfall": True, "candidates": [],
                    "message": "could not build a route through those waypoints at that distance"}
        best_err = min((c.breakdown["distance_err"] for c in chosen), default=1.0)
        return {**meta, "method": method, "pad": True, "over_spine": False,
                "filter_yield": len(filtered_loops),
                "shortfall": best_err > tolerance,
                "message": (f"closest route was {chosen[0].distance_mi:.2f} mi (target "
                            f"{target_mi:.1f} mi)" if best_err > tolerance else None),
                "candidates": [_serialize(c, r) for r, c in enumerate(chosen, 1)]}


def _seeded_seeds(n: int) -> list[int]:
    """Deterministic-but-varied seed set for the filter fan-out. (Random is fine; we keep it
    simple and reproducible-per-process by spacing seeds rather than sampling.)"""
    import random
    return random.Random().sample(range(1, 10_000_000), n)


async def _decompose(client: httpx.AsyncClient, start_ll, wp_ll, snapped_wp_ll,
                     target_m: float, cmodel: dict | None, k: int,
                     tolerance: float) -> list[Candidate]:
    """Construct out→loop→back: spine start→…→wp_L, a round_trip at wp_L sized to the
    remaining budget, then the spine reversed. Pad-loop seeds give variety; each composite
    is verified to still pass within ε of every waypoint before being kept."""
    from .generator import round_trip_body

    out = await _route(client, [start_ll, *wp_ll], cmodel)  # one-way spine
    if out is None:
        return []
    back = _reverse_path(out)
    d_out = float(out.get("distance", 0.0))
    pad_budget = target_m - 2 * d_out
    anchor = snapped_wp_ll[-1]  # round_trip anchored at the LAST waypoint

    # If the out-and-back already ≈ target, the spine alone is the answer (no pad loop).
    if pad_budget < MIN_PAD_M:
        merged = _merge_paths([out, back])
        c = _make_candidate(seed=-1, gen_distance_m=int(round(target_m)), path=merged)
        return [c] if passes_through(c.coords, snapped_wp_ll, EPS_M) else []

    # Fire several pad-loop seeds anchored at wp_L; build a composite per distinct loop.
    seeds = _seeded_seeds(DECOMP_PAD_SEEDS)
    bodies = [round_trip_body(s, int(pad_budget), (anchor[1], anchor[0]), cmodel) for s in seeds]

    async def _pad(body):
        try:
            r = await client.post(f"{GH}/route", json=body)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        ps = r.json().get("paths")
        return ps[0] if ps else None

    pad_paths = [p for p in await asyncio.gather(*[_pad(b) for b in bodies]) if p]
    cands: list[Candidate] = []
    for i, pad_path in enumerate(pad_paths):
        merged = _merge_paths([out, pad_path, back])
        c = _make_candidate(seed=seeds[i], gen_distance_m=int(round(target_m)), path=merged)
        # The spine guarantees the waypoints; re-check defensively (the loop must not have
        # produced degenerate geometry) before trusting the composite.
        if passes_through(c.coords, snapped_wp_ll, EPS_M):
            cands.append(c)
    return cands
