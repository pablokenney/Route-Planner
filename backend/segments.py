"""Strava-segment bias (PLAN.md §Feature 3).

Loads pre-resolved segment geometry from ``data/segments.json`` (produced offline by
``scripts/fetch_segments.py`` — scraping is NEVER in the request path) and detects which
known segments a candidate route traverses. The generator uses this to weight segment-rich
routes above plain ones (a gentle, distance-subordinate nudge — see ``generator.W_SEG``).

A segment counts as "traversed" when most of its polyline runs within ``EPS_M`` of the
route. This module is a dependency-graph LEAF — it imports nothing from the rest of the
backend (its own point-to-polyline geometry lives here) so ``generator`` can import it
without an import cycle.
"""
from __future__ import annotations

import json
import math
import os

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "segments.json")

# A route vertex within EPS_M of a segment vertex counts as "on" the segment. A snapped
# running route and a segment that shares the same edge pass within a few metres; 25 m
# admits true overlap while rejecting a parallel street one block over.
EPS_M = 25.0
# Fraction of a segment's sampled points that must lie on the route to call it traversed.
COVER_FRAC = 0.8
# Cap on how many points along a segment we test (long segments are sub-sampled for speed).
MAX_SAMPLE = 16


# ----------------------------------------------------------------------------- geometry
# All geometry works in [lon, lat] order to match generator.Candidate.coords.
def _pt_seg_dist_m(p, a, b) -> float:
    """Distance (m) from point p to segment a-b, all [lon, lat]. Local equirectangular
    projection at p's latitude — accurate at the street-edge scale."""
    lat0 = math.radians(p[1])
    mlon = 111320.0 * math.cos(lat0)
    mlat = 110540.0
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


def _min_dist_to_polyline_m(pt, coords) -> float:
    """Min distance (m) from pt to a polyline; pt and coords entries are [lon, lat, (ele)]."""
    if len(coords) < 2:
        return float("inf") if not coords else _pt_seg_dist_m(pt, coords[0], coords[0])
    return min(_pt_seg_dist_m(pt, coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def _bbox(points_lonlat) -> tuple[float, float, float, float]:
    lons = [p[0] for p in points_lonlat]
    lats = [p[1] for p in points_lonlat]
    return (min(lons), min(lats), max(lons), max(lats))


def _bbox_overlaps(a, b, margin_deg: float = 0.001) -> bool:
    """Do two (minlon, minlat, maxlon, maxlat) bboxes overlap, with a small margin (~110 m)?"""
    return not (a[0] > b[2] + margin_deg or a[2] < b[0] - margin_deg
                or a[1] > b[3] + margin_deg or a[3] < b[1] - margin_deg)


# --------------------------------------------------------------------- segment loading
def _prepare(raw: list) -> list[dict]:
    """Normalize a raw segments.json list into the runtime shape: each entry carries a
    `points` list in [lon, lat] order and a precomputed `bbox`. Entries without usable
    geometry are dropped (they can't be matched)."""
    out: list[dict] = []
    for s in raw:
        latlngs = s.get("latlngs") or []
        if len(latlngs) < 2:
            continue
        pts = [[float(p[1]), float(p[0])] for p in latlngs]  # [lat,lon] -> [lon,lat]
        out.append({
            "id": s.get("id"),
            "name": s.get("name", ""),
            "distance_m": float(s.get("distance_m", 0.0)),
            "points": pts,
            "bbox": s.get("bbox") or list(_bbox(pts)),
        })
    return out


def _load() -> list[dict]:
    try:
        with open(DATA) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return _prepare(data if isinstance(data, list) else [])


# Loaded once at import (mirrors the geocode-cache pattern). Empty when the cache is absent
# (e.g. tests, fresh checkout) — segment matching then returns nothing and the W_SEG term
# is inert, so the generator behaves exactly as before.
_SEGMENTS = _load()


def _sample(points: list) -> list:
    if len(points) <= MAX_SAMPLE:
        return points
    step = len(points) / MAX_SAMPLE
    return [points[int(i * step)] for i in range(MAX_SAMPLE)]


def segments_on_route(coords, segments: list[dict] | None = None,
                      eps_m: float = EPS_M, cover_frac: float = COVER_FRAC) -> list[dict]:
    """Return the known segments that `coords` traverses.

    coords: the route polyline as [lon, lat, (ele)] (generator.Candidate.coords order).
    segments: override the module cache (used by tests with a synthetic fixture).
    A segment matches when >= cover_frac of its sampled points lie within eps_m of the route.
    Each returned dict is {id, name, distance_m, covered_m}.
    """
    segs = _SEGMENTS if segments is None else segments
    if not segs or len(coords) < 2:
        return []
    rbox = _bbox(coords)
    matched: list[dict] = []
    for s in segs:
        if not _bbox_overlaps(rbox, s["bbox"]):
            continue
        pts = _sample(s["points"])
        hits = sum(1 for p in pts if _min_dist_to_polyline_m(p, coords) <= eps_m)
        if hits / len(pts) >= cover_frac:
            matched.append({"id": s["id"], "name": s["name"],
                            "distance_m": round(s["distance_m"], 1),
                            "covered_m": round(s["distance_m"], 1)})
    return matched
