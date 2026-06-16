"""Phase 5 — FastAPI over the loop generator: start points, geocoding, surface preference,
saved starts, result caching, and milestone mode (exact waypoints, padded distance).

/api/routes     -> ranked, in-band, de-duplicated candidate loops (drives the candidate UI).
                   Accepts a surface preference; results are cached (bypass with fresh=true).
/api/route      -> the single best candidate (kept for simple callers).
/api/gpx        -> GPX for a loop, reproduced from (start, gen_distance_m, seed, surface).
/api/geocode    -> address -> {lat, lon, display_name} via Nominatim (cached).
/api/starts     -> list / save / delete saved start points (server-side starts.json).
/api/milestone  -> loops through required waypoint(s), padded to a target (or derived).
/api/milestones -> list / save / delete saved milestones (server-side milestones.json).
"""
from __future__ import annotations

import json
import math
import os
import time

import gpxpy
import gpxpy.gpx
import requests
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .generator import (
    HOME,
    MI,
    SURFACE_CHOICES,
    generate,
    parse_instructions,
    round_trip_body,
    surface_custom_model,
    synthesize_turns,
)
from .milestone import milestone as milestone_generate
from .outback import out_and_backs

GH = os.environ.get("GH_URL", "http://localhost:8989")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEOCODE_CACHE = os.path.join(ROOT, "geocode_cache.json")
STARTS_FILE = os.path.join(ROOT, "starts.json")
MILESTONES_FILE = os.path.join(ROOT, "milestones.json")
DEFAULT_N = 25

app = FastAPI(title="Route Planner — Phase 5")


# --------------------------------------------------------------------- routes cache
# Small in-memory TTL cache. Generation fires ~25+ round_trip calls and is the slow path;
# the UI re-requests the same (start, distance, surface) often (preset toggling, returning
# to a saved start). Keyed on the inputs that change the candidate SET. `fresh=true` bypasses
# it so a user can still ask for new variety. In-process only — fine for a single-user tool;
# resets on restart. Not used for /api/route or /api/gpx (cheap / must stay reproducible).
_ROUTES_CACHE: dict[tuple, tuple[float, dict]] = {}
_ROUTES_TTL_S = 15 * 60
_ROUTES_CACHE_MAX = 128


def _cache_key(miles, lat, lon, n, tolerance, k, surface, out_back) -> tuple:
    # Round coords to ~11 m so trivially-different geocodes of the same place share a result.
    return (round(miles, 2), round(lat, 4), round(lon, 4), n, round(tolerance, 3), k,
            surface, out_back)


def _cache_get(key: tuple) -> dict | None:
    hit = _ROUTES_CACHE.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > _ROUTES_TTL_S:
        _ROUTES_CACHE.pop(key, None)
        return None
    return value


def _cache_put(key: tuple, value: dict) -> None:
    if len(_ROUTES_CACHE) >= _ROUTES_CACHE_MAX:
        # Evict the oldest entry (cache is tiny; a full scan is cheaper than an LRU structure).
        oldest = min(_ROUTES_CACHE, key=lambda k: _ROUTES_CACHE[k][0])
        _ROUTES_CACHE.pop(oldest, None)
    _ROUTES_CACHE[key] = (time.time(), value)


@app.get("/api/routes")
async def routes(
    miles: float = Query(5.0, ge=0.6, le=15.0),
    lat: float = Query(HOME[0], ge=-90, le=90),
    lon: float = Query(HOME[1], ge=-180, le=180),
    n: int = Query(DEFAULT_N, ge=4, le=50),
    tolerance: float = Query(0.08, ge=0.02, le=0.25),
    k: int = Query(5, ge=1, le=8),
    surface: str = Query("any"),
    out_back: bool = Query(True),
    fresh: bool = Query(False),
):
    """Ranked in-band candidate loops from (lat, lon). Honest shortfall flag if none hit tolerance.

    surface: 'any' (default), 'paved', or 'unpaved' — a soft preference (PLAN.md §Phase 4).
    out_back: also generate pure out-and-back routes and rank them alongside loops (default
              on); each carries route_type and overlap_pct so the UI can flag retracing.
    fresh=true bypasses the result cache to draw new variety.
    """
    surface = surface.lower()
    if surface not in SURFACE_CHOICES:
        raise HTTPException(422, f"surface must be one of {SURFACE_CHOICES}")

    key = _cache_key(miles, lat, lon, n, tolerance, k, surface, out_back)
    if not fresh:
        cached = _cache_get(key)
        if cached is not None:
            return {**cached, "cached": True}

    try:
        extra = (await out_and_backs(start=(lat, lon), target_m=miles * MI, surface=surface)
                 if out_back else None)
        result = await generate(miles * MI, n=n, tolerance=tolerance, k=k,
                                start=(lat, lon), surface=surface, extra_candidates=extra)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Generation failed (is GraphHopper up?): {e}")
    if not result["candidates"]:
        raise HTTPException(404, "No loops found for that distance from this start point")
    result["start"] = [lat, lon]
    result["cached"] = False
    _cache_put(key, result)
    return result


@app.get("/api/route")
async def route(distance_m: int = Query(5000, ge=1000, le=25000),
                lat: float = Query(HOME[0]), lon: float = Query(HOME[1]),
                surface: str = Query("any")):
    """Single best candidate (old shape + seed/gen_distance_m/surface so GPX can reproduce it)."""
    surface = surface.lower()
    if surface not in SURFACE_CHOICES:
        raise HTTPException(422, f"surface must be one of {SURFACE_CHOICES}")
    result = await generate(distance_m, n=DEFAULT_N, k=1, start=(lat, lon), surface=surface)
    if not result["candidates"]:
        raise HTTPException(404, "No route found")
    c = result["candidates"][0]
    return {
        "distance_m": c["distance_m"], "distance_mi": c["distance_mi"],
        "ascend_m": c["ascend_m"], "descend_m": c["descend_m"],
        "latlngs": c["latlngs"], "elevations": c["elevations"],
        "road_mix_pct": c["road_mix_pct"], "seed": c["seed"],
        "gen_distance_m": c["gen_distance_m"], "shortfall": result["shortfall"],
        "message": result["message"], "surface": result["surface"], "start": [lat, lon],
    }


def _round_trip(distance_m: int, seed: int, start, surface: str,
                instructions: bool = False) -> dict:
    """Synchronous single round_trip — used only to reproduce a chosen loop for GPX/directions.

    Must use the SAME profile + surface custom model the candidate was generated with, or
    the reproduced geometry drifts from what the user picked. POST carries the custom model.
    instructions=True additionally asks GraphHopper for the turn-by-turn list.
    """
    try:
        r = requests.post(f"{GH}/route",
                          json=round_trip_body(seed, distance_m, start,
                                               surface_custom_model(surface),
                                               instructions=instructions),
                          timeout=60)
    except requests.ConnectionError:
        raise HTTPException(503, "GraphHopper not reachable at :8989")
    if r.status_code != 200:
        raise HTTPException(502, f"GraphHopper error: {r.text[:300]}")
    paths = r.json().get("paths", [])
    if not paths:
        raise HTTPException(404, "No route found")
    return paths[0]


@app.get("/api/gpx")
def gpx(distance_m: int = Query(5000, ge=500, le=30000), seed: int = 1,
        lat: float = Query(HOME[0]), lon: float = Query(HOME[1]),
        surface: str = Query("any")):
    """GPX for a specific loop. Use a candidate's (start, gen_distance_m, seed, surface)."""
    surface = surface.lower()
    if surface not in SURFACE_CHOICES:
        raise HTTPException(422, f"surface must be one of {SURFACE_CHOICES}")
    p = _round_trip(distance_m, seed, (lat, lon), surface)
    g = gpxpy.gpx.GPX()
    g.creator = "Route Planner"
    track = gpxpy.gpx.GPXTrack(name=f"Loop ~{distance_m / MI:.1f} mi (seed {seed})")
    g.tracks.append(track)
    seg = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(seg)
    for c in p["points"]["coordinates"]:
        ele = c[2] if len(c) > 2 else None
        seg.points.append(gpxpy.gpx.GPXTrackPoint(latitude=c[1], longitude=c[0], elevation=ele))
    return Response(content=g.to_xml(), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="loop_{distance_m}m.gpx"'})


# ------------------------------------------------------------------ turn-by-turn directions
@app.get("/api/directions")
def directions(distance_m: int = Query(5000, ge=500, le=30000), seed: int = 1,
               lat: float = Query(HOME[0]), lon: float = Query(HOME[1]),
               surface: str = Query("any")):
    """Step-by-step turn list for a loop, reproduced from (start, gen_distance_m, seed,
    surface) exactly like /api/gpx but with GraphHopper instructions enabled. Returns the
    compact turn list (parse_instructions) plus the geometry so the UI/exports can place each
    cue. For milestone/out-and-back composites that no single round_trip reproduces, the UI
    instead POSTs geometry to /api/directions_track (instructions are requested per leg
    upstream). This GET path covers the common loop case.
    """
    surface = surface.lower()
    if surface not in SURFACE_CHOICES:
        raise HTTPException(422, f"surface must be one of {SURFACE_CHOICES}")
    p = _round_trip(distance_m, seed, (lat, lon), surface, instructions=True)
    return {
        "turns": parse_instructions(p),
        "latlngs": [[c[1], c[0]] for c in p["points"]["coordinates"]],
        "distance_m": round(float(p.get("distance", 0.0)), 1),
    }


# ------------------------------------------------------------------------- geocoding
def _load_geocode_cache() -> dict:
    try:
        with open(GEOCODE_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=2)):
    """Address -> coords via public Nominatim, cached to disk (usage-policy friendly)."""
    key = q.strip().lower()
    cache = _load_geocode_cache()
    if key in cache:
        return cache[key]
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "json", "limit": 1},
                         headers={"User-Agent": "Route-Planner/0.4 (personal running tool)"},
                         timeout=10)
        r.raise_for_status()
        arr = r.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Geocoding failed: {e}")
    if not arr:
        raise HTTPException(404, f"No match for {q!r}")
    res = {"lat": float(arr[0]["lat"]), "lon": float(arr[0]["lon"]),
           "display_name": arr[0]["display_name"]}
    cache[key] = res
    try:
        with open(GEOCODE_CACHE, "w") as f:
            json.dump(cache, f, indent=2)
    except OSError:
        pass
    return res


# --------------------------------------------------------------------- saved starts
# Server-side saved start points (PLAN.md §Phase 4) — persisted to starts.json so they
# survive restarts and are shared across browsers, mirroring the geocode-cache pattern.
# Shape on disk: [{"name": str, "lat": float, "lon": float}, ...]; `name` is the identity.
def _load_starts() -> list[dict]:
    try:
        with open(STARTS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_starts(starts: list[dict]) -> None:
    with open(STARTS_FILE, "w") as f:
        json.dump(starts, f, indent=2)


@app.get("/api/starts")
def list_starts():
    """All saved start points."""
    return _load_starts()


@app.post("/api/starts")
def save_start(payload: dict = Body(...)):
    """Save (or overwrite by name) a start point: {"name", "lat", "lon"}."""
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "name is required")
    try:
        lat, lon = float(payload["lat"]), float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "lat and lon are required numbers")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "lat/lon out of range")
    starts = [s for s in _load_starts() if s.get("name") != name]  # upsert by name
    starts.append({"name": name, "lat": lat, "lon": lon})
    starts.sort(key=lambda s: s["name"].lower())
    _save_starts(starts)
    return starts


@app.delete("/api/starts/{name}")
def delete_start(name: str):
    """Delete a saved start point by name."""
    starts = _load_starts()
    kept = [s for s in starts if s.get("name") != name]
    if len(kept) == len(starts):
        raise HTTPException(404, f"No saved start named {name!r}")
    _save_starts(kept)
    return kept


# ------------------------------------------------------------------------- milestone
def _parse_waypoints(payload: dict) -> list[tuple]:
    """Validate and extract [(lat, lon), ...] from a milestone request body."""
    raw = payload.get("waypoints")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(422, "waypoints must be a non-empty list of {lat, lon}")
    wps: list[tuple] = []
    for w in raw:
        try:
            lat, lon = float(w["lat"]), float(w["lon"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "each waypoint needs numeric lat and lon")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(422, "waypoint lat/lon out of range")
        wps.append((lat, lon))
    return wps


@app.post("/api/milestone")
async def milestone_route(payload: dict = Body(...)):
    """Loops through every required waypoint, padded to a target distance.

    Body: {
      waypoints: [{lat, lon}, ...]   (required, in visiting order),
      lat, lon                       (start; defaults to HOME),
      miles                          (target; ignored when pad=false),
      surface                        ('any'|'paved'|'unpaved'),
      pad                            (true=pad to target, false=derived distance),
      out_back                       (true=out-and-back retrace through waypoints, not a loop),
      eps_m, tolerance, k            (optional tunables)
    }
    Returns the core candidate shape plus milestone metadata: which method fired
    (filter/decompose/derived/out_and_back/over_spine), snapped waypoints, D_spine, and the
    honest over_spine flag when the waypoints are already farther apart than the target.
    """
    wps = _parse_waypoints(payload)
    lat = float(payload.get("lat", HOME[0]))
    lon = float(payload.get("lon", HOME[1]))
    surface = str(payload.get("surface", "any")).lower()
    if surface not in SURFACE_CHOICES:
        raise HTTPException(422, f"surface must be one of {SURFACE_CHOICES}")
    pad = bool(payload.get("pad", True))
    out_back = bool(payload.get("out_back", False))
    miles = payload.get("miles", None)
    target_m = float(miles) * MI if (pad and miles is not None) else None
    if pad and target_m is None:
        raise HTTPException(422, "miles is required when pad=true")
    if target_m is not None and not (0.6 * MI <= target_m <= 15 * MI):
        raise HTTPException(422, "miles must be between 0.6 and 15")
    eps_m = float(payload.get("eps_m", 40.0))
    tolerance = float(payload.get("tolerance", 0.08))
    k = int(payload.get("k", 5))

    try:
        result = await milestone_generate(
            start=(lat, lon), waypoints=wps, target_m=target_m, surface=surface,
            pad=pad, eps_m=eps_m, tolerance=tolerance, k=k, out_back=out_back)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Milestone generation failed (is GraphHopper up?): {e}")
    if result.get("error"):
        raise HTTPException(503, result["error"])
    return result


# ----------------------------------------------------------------- saved milestones
# Same persistence pattern as saved starts. A milestone is a named set of waypoints plus
# an optional default target distance, so favourite anchors (Dickinson fields, LeTort,
# Children's Lake, solar arrays) can be re-run with one click (PLAN.md §Phase 5 UX).
def _load_milestones() -> list[dict]:
    try:
        with open(MILESTONES_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_milestones(ms: list[dict]) -> None:
    with open(MILESTONES_FILE, "w") as f:
        json.dump(ms, f, indent=2)


@app.get("/api/milestones")
def list_milestones():
    """All saved milestones."""
    return _load_milestones()


@app.post("/api/milestones")
def save_milestone(payload: dict = Body(...)):
    """Save (or overwrite by name) a milestone: {name, waypoints:[{lat,lon}], miles?}."""
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "name is required")
    wps = _parse_waypoints(payload)
    miles = payload.get("miles", None)
    entry = {"name": name, "waypoints": [{"lat": la, "lon": lo} for la, lo in wps]}
    if miles is not None:
        entry["miles"] = float(miles)
    ms = [m for m in _load_milestones() if m.get("name") != name]  # upsert by name
    ms.append(entry)
    ms.sort(key=lambda m: m["name"].lower())
    _save_milestones(ms)
    return ms


@app.delete("/api/milestones/{name}")
def delete_milestone(name: str):
    """Delete a saved milestone by name."""
    ms = _load_milestones()
    kept = [m for m in ms if m.get("name") != name]
    if len(kept) == len(ms):
        raise HTTPException(404, f"No saved milestone named {name!r}")
    _save_milestones(kept)
    return kept


# --------------------------------------------------------------------- GPX from track
@app.post("/api/gpx_track")
def gpx_track(payload: dict = Body(...)):
    """Build GPX directly from a candidate's geometry: {latlngs:[[lat,lon],...],
    elevations:[m|null,...], name?}. Used for milestone candidates — especially decomposed
    composites that a single round_trip cannot reproduce, so reproduction-by-params (the
    core /api/gpx) does not apply. Geometry round-trips losslessly from the response we sent.
    """
    latlngs = payload.get("latlngs")
    if not isinstance(latlngs, list) or len(latlngs) < 2:
        raise HTTPException(422, "latlngs must be a list of at least two [lat, lon] points")
    elevations = payload.get("elevations") or []
    name = str(payload.get("name", "Milestone loop"))
    g = gpxpy.gpx.GPX()
    g.creator = "Route Planner"
    track = gpxpy.gpx.GPXTrack(name=name)
    g.tracks.append(track)
    seg = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(seg)
    for i, pt in enumerate(latlngs):
        ele = elevations[i] if i < len(elevations) else None
        seg.points.append(gpxpy.gpx.GPXTrackPoint(latitude=pt[0], longitude=pt[1], elevation=ele))
    fname = name.lower().replace(" ", "_")[:40] or "milestone"
    return Response(content=g.to_xml(), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.gpx"'})


@app.post("/api/directions_track")
def directions_track(payload: dict = Body(...)):
    """Turn list synthesized from arbitrary geometry: {latlngs:[[lat,lon],...]}. For
    out-and-back and milestone composites that no single round_trip reproduces, so GH
    instructions aren't available — bend detection (synthesize_turns) stands in, exactly as a
    watch app would. Returns the same turn shape as GET /api/directions."""
    latlngs = payload.get("latlngs")
    if not isinstance(latlngs, list) or len(latlngs) < 2:
        raise HTTPException(422, "latlngs must be a list of at least two [lat, lon] points")
    coords = [[pt[1], pt[0]] for pt in latlngs]  # [lat,lon] -> [lon,lat]
    return {"turns": synthesize_turns(coords)}


# ----------------------------------------------------------------- TCX (Apple Watch) export
# GraphHopper turn `sign` -> TCX CoursePoint PointType. TCX PointTypes are a fixed enum;
# we map to the closest navigation cue. Distance-tagged course points are unambiguous on
# loops/out-and-backs where a lat/lon recurs (GPX route-points are not) — PLAN.md §Feature 4.
_SIGN_TO_POINTTYPE = {
    -3: "Left", -2: "Left", -1: "Left",
    0: "Straight", 1: "Right", 2: "Right", 3: "Right",
    4: "Generic",   # arrive / finish
    5: "Generic", 6: "Generic", 7: "Straight", -7: "Straight",
}


def _tcx_xml(latlngs: list, elevations: list, turns: list, name: str) -> str:
    """Hand-build a minimal TCX Course (schema is small; no dependency needed). A <Track> of
    Trackpoints with cumulative DistanceMeters + position + altitude, and <CoursePoint>s for
    turns placed at their cumulative distance and nearest trackpoint. Importable by
    WorkOutDoors / Footpath etc. for on-watch turn-by-turn."""
    from xml.sax.saxutils import escape

    def _hav_m(a, b):  # local haversine on [lat, lon]
        R = 6371000.0
        la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
        h = (math.sin((la2 - la1) / 2) ** 2
             + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
        return 2 * R * math.asin(math.sqrt(h))

    # Cumulative distance at each trackpoint (drives DistanceMeters and course-point placement).
    cum = [0.0]
    for i in range(1, len(latlngs)):
        cum.append(cum[-1] + _hav_m(latlngs[i - 1], latlngs[i]))
    total = cum[-1]
    # A synthetic, monotonically increasing time axis (TCX requires <Time>); 1 s per metre is
    # arbitrary but valid and keeps course points orderable. No real pace is implied.
    t0 = "2024-01-01T00:00:00Z"

    def _iso(sec: float) -> str:
        s = int(sec)
        h, rem = divmod(s, 3600)
        m, ss = divmod(rem, 60)
        return f"2024-01-01T{h:02d}:{m:02d}:{ss:02d}Z"

    pts = []
    for i, (lat, lon) in enumerate(latlngs):
        ele = elevations[i] if i < len(elevations) and elevations[i] is not None else None
        ele_xml = f"<AltitudeMeters>{ele}</AltitudeMeters>" if ele is not None else ""
        pts.append(
            f"<Trackpoint><Time>{_iso(cum[i])}</Time>"
            f"<Position><LatitudeDegrees>{lat}</LatitudeDegrees>"
            f"<LongitudeDegrees>{lon}</LongitudeDegrees></Position>"
            f"{ele_xml}<DistanceMeters>{cum[i]:.1f}</DistanceMeters></Trackpoint>")

    cps = []
    for tn in turns or []:
        cm = float(tn.get("cumulative_m", 0.0))
        # nearest trackpoint index by cumulative distance (course points reference a Time).
        idx = min(range(len(cum)), key=lambda j: abs(cum[j] - cm))
        lat, lon = latlngs[idx]
        ptype = _SIGN_TO_POINTTYPE.get(int(tn.get("sign", 0)), "Generic")
        notes = escape(str(tn.get("text", "")))[:80] or ptype
        cps.append(
            f"<CoursePoint><Name>{notes[:10] or ptype}</Name><Time>{_iso(cum[idx])}</Time>"
            f"<Position><LatitudeDegrees>{lat}</LatitudeDegrees>"
            f"<LongitudeDegrees>{lon}</LongitudeDegrees></Position>"
            f"<PointType>{ptype}</PointType><Notes>{notes}</Notes></CoursePoint>")

    cname = escape(name)[:64] or "Route"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<TrainingCenterDatabase '
        'xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2 '
        'http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd">'
        f'<Courses><Course><Name>{cname}</Name>'
        f'<Lap><TotalTimeSeconds>{total:.0f}</TotalTimeSeconds>'
        f'<DistanceMeters>{total:.1f}</DistanceMeters>'
        '<BeginPosition>'
        f'<LatitudeDegrees>{latlngs[0][0]}</LatitudeDegrees>'
        f'<LongitudeDegrees>{latlngs[0][1]}</LongitudeDegrees></BeginPosition>'
        '<EndPosition>'
        f'<LatitudeDegrees>{latlngs[-1][0]}</LatitudeDegrees>'
        f'<LongitudeDegrees>{latlngs[-1][1]}</LongitudeDegrees></EndPosition></Lap>'
        f'<Track>{"".join(pts)}</Track>{"".join(cps)}'
        '</Course></Courses></TrainingCenterDatabase>')


@app.post("/api/tcx_track")
def tcx_track(payload: dict = Body(...)):
    """Build a TCX Course from geometry + turn cues for Apple Watch turn-by-turn (PLAN.md
    §Feature 4): {latlngs:[[lat,lon],...], elevations:[m|null,...], turns:[...]?, name?}.
    If `turns` is omitted, they are synthesized from the geometry (bend detection). Course
    points carry distance/Time so they stay unambiguous on retraced out-and-backs."""
    latlngs = payload.get("latlngs")
    if not isinstance(latlngs, list) or len(latlngs) < 2:
        raise HTTPException(422, "latlngs must be a list of at least two [lat, lon] points")
    elevations = payload.get("elevations") or []
    name = str(payload.get("name", "Route"))
    turns = payload.get("turns")
    if not turns:
        coords = [[pt[1], pt[0]] for pt in latlngs]
        turns = synthesize_turns(coords)
    xml = _tcx_xml(latlngs, elevations, turns, name)
    fname = name.lower().replace(" ", "_")[:40] or "route"
    return Response(content=xml, media_type="application/vnd.garmin.tcx+xml",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.tcx"'})


# ---------------------------------------------------------------------------- static
@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "frontend", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "graphhopper": GH}
