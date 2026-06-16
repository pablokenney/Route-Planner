"""Tiny GraphHopper client + analysis helpers shared by the route-rule tests.
Requires a running GraphHopper (scripts/run_graphhopper.sh) on :8989.
"""
from __future__ import annotations

import math
from typing import Optional

import requests

GH = "http://localhost:8989"
HOME = (40.195016, -77.199929)  # lat, lon
EXCLUDED_CLASSES = {"motorway", "trunk", "primary", "secondary"}
SPEED_HI = 45        # km/h; rule excludes explicit max_speed > 45
SPEED_SENTINEL = 200  # km/h; >= this is treated as untagged/unlimited (kept)
PREFERRED = {"footway", "path", "cycleway", "pedestrian"}


def route(a, b, profile, details=("road_class", "max_speed"), **extra) -> requests.Response:
    params = {
        "point": [f"{a[0]},{a[1]}", f"{b[0]},{b[1]}"],
        "profile": profile, "ch.disable": "true",
        "details": list(details), "points_encoded": "false", "instructions": "false",
    }
    params.update(extra)
    return requests.get(f"{GH}/route", params=params, timeout=30)


def route_path(a, b, profile, **extra) -> Optional[dict]:
    """Return paths[0], or None if GraphHopper found no route (status != 200)."""
    r = route(a, b, profile, **extra)
    return r.json()["paths"][0] if r.status_code == 200 else None


def round_trip_path(distance_m: int, seed: int, profile="run", start=HOME) -> Optional[dict]:
    r = requests.get(f"{GH}/route", params={
        "point": f"{start[0]},{start[1]}", "profile": profile, "algorithm": "round_trip",
        "round_trip.distance": distance_m, "round_trip.seed": seed, "ch.disable": "true",
        "details": ["road_class", "max_speed"], "points_encoded": "false", "instructions": "false",
    }, timeout=60)
    return r.json()["paths"][0] if r.status_code == 200 else None


def seg_values(path: dict, key: str) -> list:
    return [s[2] for s in path.get("details", {}).get(key, [])]


def numeric(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def classes_in(path: dict) -> set[str]:
    return {str(c).lower() for c in seg_values(path, "road_class")}


def overspeed_in(path: dict) -> list[float]:
    """Edges with an explicit stored max_speed in (45, 200) — i.e. rule violations."""
    return [s for s in (numeric(v) for v in seg_values(path, "max_speed"))
            if s is not None and SPEED_HI < s < SPEED_SENTINEL]


def _hav(p, q) -> float:
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [p[1], p[0], q[1], q[0]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def length_by_class(path: dict) -> dict[str, float]:
    cs = path["points"]["coordinates"]
    out: dict[str, float] = {}
    for fr, to, cls in path["details"]["road_class"]:
        seg = sum(_hav(cs[i], cs[i + 1]) for i in range(fr, to))
        out[str(cls).lower()] = out.get(str(cls).lower(), 0.0) + seg
    return out


def preferred_fraction(path: dict) -> float:
    lbc = length_by_class(path)
    total = sum(lbc.values())
    return sum(v for k, v in lbc.items() if k in PREFERRED) / total if total else 0.0


def max_stored_speed(path: dict) -> Optional[float]:
    vals = [numeric(v) for v in seg_values(path, "max_speed")]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None
