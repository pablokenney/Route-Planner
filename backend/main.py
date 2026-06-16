"""Phase 0 walking skeleton — FastAPI that proxies ONE round_trip to GraphHopper and
exports GPX. No scoring, no seed-iteration, no candidate ranking (those are later phases).
"""
from __future__ import annotations

import io
import os

import gpxpy
import gpxpy.gpx
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

GH = os.environ.get("GH_URL", "http://localhost:8989")
HOME = (40.195016, -77.199929)  # lat, lon — confirmed start point
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="Route Planner — Phase 0")


def _round_trip(distance_m: int, seed: int) -> dict:
    """Call GraphHopper round_trip and return paths[0] with elevation + geometry."""
    try:
        r = requests.get(f"{GH}/route", params={
            "point": f"{HOME[0]},{HOME[1]}",
            "profile": "run",
            "algorithm": "round_trip",
            "round_trip.distance": distance_m,
            "round_trip.seed": seed,
            "ch.disable": "true",
            "elevation": "true",
            "points_encoded": "false",
            "instructions": "false",
        }, timeout=60)
    except requests.ConnectionError:
        raise HTTPException(503, "GraphHopper not reachable at :8989 — start it with scripts/run_graphhopper.sh")
    if r.status_code != 200:
        raise HTTPException(502, f"GraphHopper error: {r.text[:300]}")
    paths = r.json().get("paths", [])
    if not paths:
        raise HTTPException(404, "No route found")
    return paths[0]


@app.get("/api/route")
def route(distance_m: int = Query(5000, ge=1000, le=25000), seed: int = 1):
    """Return one loop as Leaflet-friendly [lat, lon] coords + summary stats."""
    p = _round_trip(distance_m, seed)
    coords = p["points"]["coordinates"]  # [lon, lat, (ele)]
    return {
        "distance_m": round(p.get("distance", 0), 1),
        "distance_mi": round(p.get("distance", 0) / 1609.344, 2),
        "ascend_m": round(p.get("ascend", 0), 1),
        "descend_m": round(p.get("descend", 0), 1),
        "latlngs": [[c[1], c[0]] for c in coords],
        "start": list(HOME),
    }


@app.get("/api/gpx")
def gpx(distance_m: int = Query(5000, ge=1000, le=25000), seed: int = 1):
    """Build a GPX track from the loop geometry (with elevation if present)."""
    p = _round_trip(distance_m, seed)
    g = gpxpy.gpx.GPX()
    g.creator = "Route Planner (Phase 0)"
    track = gpxpy.gpx.GPXTrack(name=f"Loop {distance_m} m seed {seed}")
    g.tracks.append(track)
    seg = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(seg)
    for c in p["points"]["coordinates"]:
        ele = c[2] if len(c) > 2 else None
        seg.points.append(gpxpy.gpx.GPXTrackPoint(latitude=c[1], longitude=c[0], elevation=ele))
    return Response(
        content=g.to_xml(),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="loop_{distance_m}m.gpx"'},
    )


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "frontend", "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "graphhopper": GH}
