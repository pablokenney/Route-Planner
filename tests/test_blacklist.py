"""Unsafe-road avoidance: generated routes must never traverse a blacklisted road.

The block lives at the ROUTING layer (a GraphHopper custom-model `area` with zeroed priority
around each road, built by scripts/build_blacklist.py), NOT a post-filter that deletes whole
candidates. So these tests assert two things: (1) loops from HOME — which previously routed
onto Heisers Lane / Bonnybrook Road on longer pulls — now keep every point out of the
avoidance polygons, and (2) the block is actually present in the custom model. Skipped if
GraphHopper isn't running.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
import requests

from backend.generator import MI, generate, route_custom_model

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gh_up() -> bool:
    try:
        return requests.get("http://localhost:8989/info", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _gh_up(), reason="GraphHopper not running on :8989")


def _polys() -> list[list]:
    with open(os.path.join(ROOT, "data", "blacklist_areas.json")) as f:
        fc = json.load(f)
    return [feat["geometry"]["coordinates"][0] for feat in fc["features"]]


def _point_in_ring(pt, ring) -> bool:
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _points_inside(latlngs, polys) -> int:
    # latlngs are [lat, lon]; rings are [lon, lat].
    return sum(1 for lat, lon in latlngs
               if any(_point_in_ring((lon, lat), ring) for ring in polys))


def _run(coro):
    return asyncio.run(coro)


def test_blacklist_block_present_in_custom_model():
    m = route_custom_model("any")
    assert m and "areas" in m, "blacklist areas missing from custom model"
    assert m["areas"]["features"], "no avoidance polygons loaded"
    # a zero-priority statement referencing every area must be present
    assert any(str(s.get("multiply_by")) == "0" for s in m["priority"]), \
        "blacklisted areas are not zeroed in the priority block"


def test_generated_loops_avoid_blacklisted_roads():
    polys = _polys()
    # 8 mi from HOME is the range that previously drew onto Heisers Lane / Bonnybrook Road.
    for t in range(3):
        r = _run(generate(8 * MI, n=25, start=(40.195016, -77.199929),
                          surface="any", rng_seed=200 + t))
        for c in r["candidates"]:
            assert _points_inside(c["latlngs"], polys) == 0, (
                f"loop #{c['rank']} ({c['distance_mi']} mi) entered a blacklisted road")
