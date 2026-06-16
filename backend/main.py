"""Phase 3 — FastAPI over the loop generator, with start-point + geocoding for the UI.

/api/routes  -> ranked, in-band, de-duplicated candidate loops (drives the candidate UI).
/api/route   -> the single best candidate (kept for simple callers).
/api/gpx     -> GPX for a specific loop, reproduced from (start, gen_distance_m, seed).
/api/geocode -> address -> {lat, lon, display_name} via Nominatim (cached).
"""
from __future__ import annotations

import json
import os

import gpxpy
import gpxpy.gpx
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .generator import HOME, MI, generate

GH = os.environ.get("GH_URL", "http://localhost:8989")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEOCODE_CACHE = os.path.join(ROOT, "geocode_cache.json")
DEFAULT_N = 25

app = FastAPI(title="Route Planner — Phase 3")


@app.get("/api/routes")
async def routes(
    miles: float = Query(5.0, ge=0.6, le=15.0),
    lat: float = Query(HOME[0], ge=-90, le=90),
    lon: float = Query(HOME[1], ge=-180, le=180),
    n: int = Query(DEFAULT_N, ge=4, le=50),
    tolerance: float = Query(0.08, ge=0.02, le=0.25),
    k: int = Query(5, ge=1, le=8),
):
    """Ranked in-band candidate loops from (lat, lon). Honest shortfall flag if none hit tolerance."""
    try:
        result = await generate(miles * MI, n=n, tolerance=tolerance, k=k, start=(lat, lon))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Generation failed (is GraphHopper up?): {e}")
    if not result["candidates"]:
        raise HTTPException(404, "No loops found for that distance from this start point")
    result["start"] = [lat, lon]
    return result


@app.get("/api/route")
async def route(distance_m: int = Query(5000, ge=1000, le=25000),
                lat: float = Query(HOME[0]), lon: float = Query(HOME[1])):
    """Single best candidate (old shape + seed/gen_distance_m so GPX can reproduce it)."""
    result = await generate(distance_m, n=DEFAULT_N, k=1, start=(lat, lon))
    if not result["candidates"]:
        raise HTTPException(404, "No route found")
    c = result["candidates"][0]
    return {
        "distance_m": c["distance_m"], "distance_mi": c["distance_mi"],
        "ascend_m": c["ascend_m"], "descend_m": c["descend_m"],
        "latlngs": c["latlngs"], "elevations": c["elevations"],
        "road_mix_pct": c["road_mix_pct"], "seed": c["seed"],
        "gen_distance_m": c["gen_distance_m"], "shortfall": result["shortfall"],
        "message": result["message"], "start": [lat, lon],
    }


def _round_trip(distance_m: int, seed: int, start) -> dict:
    """Synchronous single round_trip — used only to reproduce a chosen loop for GPX."""
    try:
        r = requests.get(f"{GH}/route", params={
            "point": f"{start[0]},{start[1]}", "profile": "run", "algorithm": "round_trip",
            "round_trip.distance": distance_m, "round_trip.seed": seed, "ch.disable": "true",
            "elevation": "true", "points_encoded": "false", "instructions": "false",
        }, timeout=60)
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
        lat: float = Query(HOME[0]), lon: float = Query(HOME[1])):
    """GPX for a specific loop. Use a candidate's (start, gen_distance_m, seed) to reproduce it."""
    p = _round_trip(distance_m, seed, (lat, lon))
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
                         headers={"User-Agent": "Route-Planner/0.3 (personal running tool)"},
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


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "frontend", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "graphhopper": GH}
