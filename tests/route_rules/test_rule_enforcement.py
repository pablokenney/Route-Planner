"""Phase 1 — full rule-enforcement suite (PLAN.md §3 verification).

Complements the Phase 0 composition probe (test_composition_probe.py). Fixtures are real
ways discovered in data/carlisle.osm.pbf via `osmium export` + tag filtering, hardcoded
with provenance so the suite is reproducible without Overpass.

Requires a running GraphHopper (scripts/run_graphhopper.sh) with profiles run / foot_raw /
run_noprefs.
"""
from __future__ import annotations

import pytest
import requests

from ghclient import (
    EXCLUDED_CLASSES, GH, HOME, SPEED_HI,
    classes_in, max_stored_speed, numeric, overspeed_in,
    preferred_fraction, route_path, round_trip_path, seg_values,
)


def _gh_up() -> bool:
    try:
        return requests.get(f"{GH}/info", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _gh_up(), reason="GraphHopper not running on :8989")

# --- Fixtures from data/carlisle.osm.pbf -----------------------------------------------
RES_25 = {"name": "Granite Run",        # highway=residential, maxspeed=25 mph
          "from": (40.183437, -77.165845), "to": (40.1823014, -77.1707381)}
RES_40 = {"name": "Valley View Drive",  # highway=residential, maxspeed=40 mph (speed-excluded, not class)
          "from": (40.2548, -77.09389), "to": (40.257956, -77.076882)}
RES_UNTAGGED = {"name": "Mooreland Avenue",  # highway=residential, no maxspeed
                "from": (40.192379, -77.200191), "to": (40.199203, -77.199207)}
TERTIARY_25 = {"name": "North West Street",  # highway=tertiary, maxspeed=25 mph
               "from": (40.210443, -77.192602), "to": (40.215571, -77.191855)}

# Cross-town OD pairs that naturally span primary/secondary arterials.
CROSS_TOWN = [
    ((40.2088, -77.1896), (40.1939, -77.2163)),
    (HOME, (40.2110, -77.1620)),
    ((40.1800, -77.1700), (40.2110, -77.2240)),
]
# OD where the >1 footway/path multiplier strongly reroutes vs run_noprefs (empirically found).
PREF_OD = ((40.20879, -77.18961), (40.20328, -77.19803))


# --- 1. Highway rejection --------------------------------------------------------------
def test_highway_rejection_zero_excluded_classes():
    exercised = False  # did at least one OD naturally want an arterial unconstrained?
    for a, b in CROSS_TOWN:
        run = route_path(a, b, "run")
        assert run is not None, f"run found no route {a} -> {b}"
        bad = classes_in(run) & EXCLUDED_CLASSES
        assert not bad, f"run used excluded classes {bad} on {a} -> {b}"
        raw = route_path(a, b, "foot_raw")
        if raw and (classes_in(raw) & EXCLUDED_CLASSES):
            exercised = True
    assert exercised, "not meaningful: no OD used an arterial even unconstrained"


# --- 2. Speed rejection (excluded by speed, NOT class) ---------------------------------
def test_speed_rejection_40mph_residential():
    raw = route_path(RES_40["from"], RES_40["to"], "foot_raw")
    assert raw is not None, "could not route the 40mph street even unconstrained"
    ms = max_stored_speed(raw)
    assert ms is not None and ms > SPEED_HI, (
        f"expected {RES_40['name']} stored > {SPEED_HI} km/h, got {ms} (test not meaningful)")
    run = route_path(RES_40["from"], RES_40["to"], "run")
    # run must avoid the fast road: either route with no >45 edge, or fail closed.
    if run is not None:
        assert overspeed_in(run) == [], f"run used a >45 km/h edge: {overspeed_in(run)}"


# --- 3. Explicit-25 RESIDENTIAL admission (the everyday case the principle protects) ----
def test_25mph_residential_admitted():
    raw = route_path(RES_25["from"], RES_25["to"], "foot_raw")
    assert raw is not None
    ms = max_stored_speed(raw)
    assert ms is None or ms <= SPEED_HI, (
        f"{RES_25['name']} (25 mph residential) stored {ms} km/h > {SPEED_HI} — would be wrongly excluded")
    run = route_path(RES_25["from"], RES_25["to"], "run")
    assert run is not None, "run failed to route a 25mph residential street"
    assert "residential" in classes_in(run), (
        f"run did not use the residential street (classes: {classes_in(run)})")


# --- 4. Untagged-residential admission -------------------------------------------------
def test_untagged_residential_admitted():
    run = route_path(RES_UNTAGGED["from"], RES_UNTAGGED["to"], "run")
    assert run is not None, "run failed to route an untagged residential street"
    assert "residential" in classes_in(run), f"classes: {classes_in(run)}"
    assert overspeed_in(run) == [], "untagged residential route somehow used an over-speed edge"


# --- 5. Tertiary/unclassified last-resort ----------------------------------------------
def test_tertiary_avoided_when_residential_alternative_exists():
    run = route_path(TERTIARY_25["from"], TERTIARY_25["to"], "run")
    assert run is not None
    assert "tertiary" not in classes_in(run), (
        f"run used tertiary despite a residential alternative: {classes_in(run)}")
    assert "residential" in classes_in(run)


def test_no_tertiary_or_other_edge_exceeds_speed_limit():
    loop = round_trip_path(8000, 1)
    assert loop is not None
    # global guarantee subsumes "tertiary never appears when max_speed > 45"
    assert overspeed_in(loop) == [], f"loop contains over-speed edges: {overspeed_in(loop)}"


# --- 6. Preference multipliers actually bite -------------------------------------------
def test_preference_multipliers_bite():
    a, b = PREF_OD
    run = route_path(a, b, "run")
    noprefs = route_path(a, b, "run_noprefs")
    assert run is not None and noprefs is not None
    fr, fn = preferred_fraction(run), preferred_fraction(noprefs)
    assert fr > fn + 0.2, (
        f"preference multipliers did not change routing: run footway/path fraction "
        f"{fr:.3f} vs run_noprefs {fn:.3f}")
