"""Phase 2 — FastAPI over the loop generator.

/api/routes  -> ranked, de-duplicated top-K candidate loops (the Phase 2 deliverable).
/api/route   -> the single best candidate (generator-backed; replaces the Phase 0 proxy).
/api/gpx     -> GPX for a specific loop, reproduced from (gen_distance_m, seed).
"""
from __future__ import annotations

import os

import gpxpy
import gpxpy.gpx
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .generator import HOME, MI, generate

GH = os.environ.get("GH_URL", "http://localhost:8989")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_N = 25

app = FastAPI(title="Route Planner — Phase 2")


@app.get("/api/routes")
async def routes(
    miles: float = Query(5.0, ge=0.6, le=15.0),
    n: int = Query(DEFAULT_N, ge=4, le=50),
    tolerance: float = Query(0.08, ge=0.02, le=0.25),
    k: int = Query(5, ge=1, le=8),
):
    """Generate ranked candidate loops. Honest shortfall flag if none hit tolerance."""
    try:
        result = await generate(miles * MI, n=n, tolerance=tolerance, k=k)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Generation failed (is GraphHopper up?): {e}")
    if not result["candidates"]:
        raise HTTPException(404, "No loops found for that distance from this start point")
    return result


@app.get("/api/route")
async def route(distance_m: int = Query(5000, ge=1000, le=25000)):
    """Single best candidate (old shape + seed/gen_distance_m so GPX can reproduce it)."""
    result = await generate(distance_m, n=DEFAULT_N, k=1)
    if not result["candidates"]:
        raise HTTPException(404, "No route found")
    c = result["candidates"][0]
    return {
        "distance_m": c["distance_m"],
        "distance_mi": c["distance_mi"],
        "ascend_m": c["ascend_m"],
        "descend_m": c["descend_m"],
        "latlngs": c["latlngs"],
        "road_mix_pct": c["road_mix_pct"],
        "seed": c["seed"],
        "gen_distance_m": c["gen_distance_m"],
        "shortfall": result["shortfall"],
        "message": result["message"],
        "start": list(HOME),
    }


def _round_trip(distance_m: int, seed: int) -> dict:
    """Synchronous single round_trip — used only to reproduce a chosen loop for GPX."""
    try:
        r = requests.get(f"{GH}/route", params={
            "point": f"{HOME[0]},{HOME[1]}", "profile": "run", "algorithm": "round_trip",
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
def gpx(distance_m: int = Query(5000, ge=500, le=30000), seed: int = 1):
    """GPX track for a specific loop. Use a candidate's (gen_distance_m, seed) to reproduce it."""
    p = _round_trip(distance_m, seed)
    g = gpxpy.gpx.GPX()
    g.creator = "Route Planner"
    track = gpxpy.gpx.GPXTrack(name=f"Loop ~{distance_m/MI:.1f} mi (seed {seed})")
    g.tracks.append(track)
    seg = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(seg)
    for c in p["points"]["coordinates"]:
        ele = c[2] if len(c) > 2 else None
        seg.points.append(gpxpy.gpx.GPXTrackPoint(latitude=c[1], longitude=c[0], elevation=ele))
    return Response(content=g.to_xml(), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="loop_{distance_m}m.gpx"'})


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "frontend", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "graphhopper": GH}
