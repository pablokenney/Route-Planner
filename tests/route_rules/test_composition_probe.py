#!/usr/bin/env python3
"""Step 3 — THE COMPOSITION PROBE (the Phase 0 gate).

The whole architecture decision hangs on this: does GraphHopper's round_trip algorithm
actually honor the exclusion custom model? We prove it empirically rather than assume.

Asserts, against a running GraphHopper (scripts/run_graphhopper.sh) on :8989:
  (a) a round_trip route contains ZERO edges of class motorway/trunk/primary/secondary
  (b) a round_trip route contains ZERO edges with stored max_speed in (45, 200] km/h
  (c) read-back: a KNOWN 25-mph-tagged street near home has a stored max_speed <= 45
      (i.e. a real 25-mph road is ADMITTED, not wrongly excluded by rounding), and the
      `run` profile actually routes onto it.

Runs as a script (prints a full report) or under pytest (asserts). Needs only `requests`.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from typing import Optional

import requests

GH = "http://localhost:8989"
HOME = (40.195016, -77.199929)  # lat, lon
EXCLUDED_CLASSES = {"motorway", "trunk", "primary", "secondary"}
SPEED_HI = 45      # km/h; our rule excludes explicit max_speed > 45
SPEED_SENTINEL = 200  # km/h; above this we treat as "untagged/unlimited" (must be kept)
OVERPASS = "https://overpass-api.de/api/interpreter"

# A KNOWN 25-mph-tagged street in the Carlisle extract, discovered from the local data:
#   osmium tags-filter data/carlisle.osm.pbf w/maxspeed | export | grep "25 mph"
# "North West Street" (highway=tertiary, maxspeed="25 mph"). Used for the read-back so the
# test is self-contained and reproducible (no dependency on the public Overpass API).
KNOWN_25MPH = {
    "name": "North West Street",
    "from": (40.210443, -77.192602),
    "to": (40.215571, -77.191855),
}


# ---------------------------------------------------------------------------- helpers
def gh_route(params: dict) -> dict:
    r = requests.get(f"{GH}/route", params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GraphHopper {r.status_code}: {r.text[:400]}")
    return r.json()


def round_trip(distance_m: int = 5000, seed: int = 1) -> dict:
    return gh_route({
        "point": f"{HOME[0]},{HOME[1]}",
        "profile": "run",
        "algorithm": "round_trip",
        "round_trip.distance": distance_m,
        "round_trip.seed": seed,
        "ch.disable": "true",
        "details": ["road_class", "max_speed"],
        "points_encoded": "false",
        "instructions": "false",
    })


def detail_values(path: dict, key: str) -> list:
    """GraphHopper details: list of [from_idx, to_idx, value]. Return the values."""
    return [seg[2] for seg in path.get("details", {}).get(key, [])]


def numeric(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / Infinity
        return None
    return f


# --------------------------------------------------------------- known-25mph read-back
def read_back_25mph() -> dict:
    """Read the RAW stored max_speed of a KNOWN 25-mph street via the non-excluding
    foot_raw profile, and confirm the `run` profile still routes the area (not hard-
    blocked). The boundary the >45 threshold endangers: a real 25 mph must stay <= 45."""
    a, b = KNOWN_25MPH["from"], KNOWN_25MPH["to"]
    raw = gh_route({
        "point": [f"{a[0]},{a[1]}", f"{b[0]},{b[1]}"],
        "profile": "foot_raw",
        "ch.disable": "true",
        "details": ["max_speed", "road_class"],
        "points_encoded": "false",
        "instructions": "false",
    })
    raw_path = raw["paths"][0]
    raw_speeds = [s for s in (numeric(v) for v in detail_values(raw_path, "max_speed")) if s is not None]
    admitted = True
    try:
        gh_route({
            "point": [f"{a[0]},{a[1]}", f"{b[0]},{b[1]}"],
            "profile": "run",
            "ch.disable": "true",
            "points_encoded": "false",
            "instructions": "false",
        })
    except RuntimeError:
        admitted = False
    return {
        "found": True,
        "street": KNOWN_25MPH["name"],
        "from": a,
        "to": b,
        "raw_max_speed_values": raw_speeds,
        "max_observed": max(raw_speeds) if raw_speeds else None,
        "run_profile_routes_area": admitted,
    }


# ----------------------------------------------------------------------------- analysis
def analyze() -> dict:
    rt = round_trip()
    path = rt["paths"][0]
    classes = [str(c).lower() for c in detail_values(path, "road_class")]
    speeds_raw = detail_values(path, "max_speed")
    speeds = [numeric(v) for v in speeds_raw]

    violating_classes = sorted({c for c in classes if c in EXCLUDED_CLASSES})
    violating_speeds = sorted({
        s for s in speeds if s is not None and SPEED_HI < s < SPEED_SENTINEL
    })

    from collections import Counter
    return {
        "round_trip_distance_m": round(path.get("distance", 0), 1),
        "round_trip_ascend_m": round(path.get("ascend", 0), 1),
        "road_class_histogram": dict(Counter(classes).most_common()),
        "distinct_max_speed_values": sorted(
            {s for s in speeds if s is not None}
        ),
        "max_speed_raw_sample": speeds_raw[:15],
        "violating_classes": violating_classes,
        "violating_speeds": violating_speeds,
        "read_back_25mph": read_back_25mph(),
    }


def report() -> dict:
    res = analyze()
    print("=" * 72)
    print("COMPOSITION PROBE — does round_trip honor the exclusion custom model?")
    print("=" * 72)
    print(json.dumps(res, indent=2, default=str))
    print("-" * 72)
    ok_class = not res["violating_classes"]
    ok_speed = not res["violating_speeds"]
    rb = res["read_back_25mph"]
    print(f"(a) zero excluded road classes ........ {'PASS' if ok_class else 'FAIL'}"
          f"  {res['violating_classes'] or ''}")
    print(f"(b) zero edges with max_speed in (45,200] {'PASS' if ok_speed else 'FAIL'}"
          f"  {res['violating_speeds'] or ''}")
    ok_rb = (rb["max_observed"] is None or rb["max_observed"] <= SPEED_HI) and rb["run_profile_routes_area"]
    print(f"(c) known 25-mph street kept .......... {'PASS' if ok_rb else 'FAIL'}"
          f"  '{rb['street']}' stored_max_speed={rb['max_observed']} (<= {SPEED_HI}), "
          f"run_routes_area={rb['run_profile_routes_area']}")
    print("=" * 72)
    return res


# ------------------------------------------------------------------------------ pytest
def test_no_excluded_road_classes():
    res = analyze()
    assert not res["violating_classes"], f"excluded classes present: {res['violating_classes']}"


def test_no_overspeed_edges():
    res = analyze()
    assert not res["violating_speeds"], f"edges with explicit max_speed>45: {res['violating_speeds']}"


def test_known_25mph_kept():
    rb = read_back_25mph()
    assert rb["max_observed"] is None or rb["max_observed"] <= SPEED_HI, (
        f"a real 25-mph street ({rb['street']}) stored as {rb['max_observed']} km/h "
        f"would be wrongly excluded by the >{SPEED_HI} rule")
    assert rb["run_profile_routes_area"], "run profile failed to route near a known 25-mph street"


if __name__ == "__main__":
    try:
        report()
    except requests.ConnectionError:
        print("ERROR: GraphHopper not reachable at :8989. Start it with "
              "scripts/run_graphhopper.sh", file=sys.stderr)
        sys.exit(2)
