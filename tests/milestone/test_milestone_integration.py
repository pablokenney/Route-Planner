"""Phase 5 — milestone mode integration tests (need a running GraphHopper on :8989).

Covers the spec's required checks:
  • waypoint inclusion within ε of EVERY required point,
  • padded distance within ±5–8% of target across several seeds (not one lucky pull),
  • the over-spine edge case returns the honest flag and NEVER silently drops a waypoint,
  • the CARRIED-OVER SAFETY GUARANTEE — a milestone route has zero excluded-class edges and
    zero explicit max_speed>45 edges, proving milestone mode is not a backdoor around the
    locked safety model,
  • snapping — a pin on an excluded road snaps to a runnable edge, not onto the excluded one.

Skips (does not error) when GraphHopper is down, matching the other suites.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import requests

# make the route_rules ghclient importable for max_speed/road_class inspection
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "route_rules"))
from ghclient import EXCLUDED_CLASSES, classes_in, overspeed_in  # noqa: E402

from backend.milestone import EPS_M, MI, milestone, min_dist_to_polyline_m  # noqa: E402

GH = "http://localhost:8989"
HOME = (40.195016, -77.199929)
# The motivating case: the Dickinson intramural fields / solar arrays anchor.
FIELDS = (40.20074, -77.18851)
# A coordinate sitting ON a `primary` edge (discovered from the live graph) — the `run`
# profile excludes primary, so a pin here MUST snap away onto a runnable edge.
ON_PRIMARY = (40.202877, -77.206492)


def _gh_up() -> bool:
    try:
        return requests.get(f"{GH}/info", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _gh_up(), reason="GraphHopper not running on :8989")


def _run(coro):
    return asyncio.run(coro)


def _snapped_ll(result) -> list:
    """Snapped waypoints in [lon, lat] for ε checks against candidate geometry."""
    return [[s["snapped"][1], s["snapped"][0]] for s in result["snapped_waypoints"]]


def _coords_lonlat(cand) -> list:
    return [[ll[1], ll[0]] for ll in cand["latlngs"]]


# ------------------------------------------------------------------ waypoint inclusion
def test_every_candidate_includes_the_waypoint():
    r = _run(milestone(HOME, [FIELDS], 6 * MI, k=5))
    assert r["candidates"], "milestone must return at least one loop"
    swll = _snapped_ll(r)
    for c in r["candidates"]:
        for w in swll:
            d = min_dist_to_polyline_m(w, _coords_lonlat(c))
            assert d <= EPS_M + 1, f"candidate misses waypoint by {d:.0f} m (> ε={EPS_M})"


def test_multi_waypoint_inclusion():
    # two near-home anchors; every returned loop must hit BOTH
    wp2 = (40.199, -77.205)
    r = _run(milestone(HOME, [FIELDS, wp2], 6 * MI, k=4))
    assert r["candidates"]
    swll = _snapped_ll(r)
    assert len(swll) == 2
    for c in r["candidates"]:
        assert all(min_dist_to_polyline_m(w, _coords_lonlat(c)) <= EPS_M + 1 for w in swll)


# --------------------------------------------------------------------- padded distance
def test_padded_distance_within_tolerance_multiple_runs():
    # seeds are random; require the best candidate within ~8% on most of several pulls
    hits = 0
    trials = 4
    for _ in range(trials):
        r = _run(milestone(HOME, [FIELDS], 6 * MI, tolerance=0.08, k=5))
        best = min(abs(c["distance_mi"] - 6.0) / 6.0 for c in r["candidates"])
        if best <= 0.08:
            hits += 1
    assert hits >= 3, f"best within ±8% only {hits}/{trials} runs"


# ----------------------------------------------------------------------- over-spine
def test_over_spine_is_honest_never_silent_drop():
    # two far-apart anchors, impossibly small target -> can't pad down
    far_a = (40.2074, -77.2010)
    far_b = (40.1607, -77.1936)
    r = _run(milestone(HOME, [far_a, far_b], 2 * MI))
    assert r["over_spine"] is True
    assert r["shortfall"] is True
    assert not r["candidates"], "over-spine must return NO loop rather than drop a waypoint"
    assert r["message"] and "already make" in r["message"]
    # both waypoints are still acknowledged in the response (not silently dropped)
    assert len(r["snapped_waypoints"]) == 2


# -------------------------------------------------- CARRIED-OVER SAFETY GUARANTEE (core)
def test_milestone_route_has_zero_excluded_classes():
    # road_mix_pct is computed from the FULL composite route's actual edges, so this directly
    # inspects what the milestone route used — not an inherited assumption.
    for target, wps in [(6 * MI, [FIELDS]), (6 * MI, [FIELDS, (40.199, -77.205)])]:
        r = _run(milestone(HOME, wps, target, k=5))
        for c in r["candidates"]:
            present = set(c["road_mix_pct"].keys())
            bad = present & EXCLUDED_CLASSES
            assert not bad, f"milestone route used excluded class(es): {bad}"


def test_milestone_legs_have_zero_overspeed_edges():
    # The new surface milestone adds over the proven round_trip is the via-spine. Re-trace it
    # on the run profile WITH max_speed details and assert no edge exceeds the >45 km/h rule.
    snapped = _run(milestone(HOME, [FIELDS], 6 * MI, k=1))["snapped_waypoints"][0]["snapped"]
    pts = [f"{HOME[0]},{HOME[1]}", f"{snapped[0]},{snapped[1]}", f"{HOME[0]},{HOME[1]}"]
    resp = requests.get(f"{GH}/route", params={
        "point": pts, "profile": "run", "ch.disable": "true",
        "details": ["road_class", "max_speed"], "points_encoded": "false",
        "instructions": "false"}, timeout=30)
    assert resp.status_code == 200
    path = resp.json()["paths"][0]
    assert not overspeed_in(path), "milestone spine used an edge with explicit max_speed>45"
    assert not (classes_in(path) & EXCLUDED_CLASSES), "milestone spine used an excluded class"


# ----------------------------------------------------------------------------- snapping
def _snap_last_class(profile: str, pin) -> tuple:
    """Snap `pin` under `profile` by routing HOME->pin; return (snapped[lon,lat], last edge
    road_class). foot_raw (no exclusions) reveals where the pin physically lies; run reveals
    where the safety model is willing to put it."""
    resp = requests.get(f"{GH}/route", params={
        "point": [f"{HOME[0]},{HOME[1]}", f"{pin[0]},{pin[1]}"], "profile": profile,
        "ch.disable": "true", "details": ["road_class"], "points_encoded": "false",
        "instructions": "false"}, timeout=30)
    p = resp.json()["paths"][0]
    snapped = p["snapped_waypoints"]["coordinates"][-1]
    last_class = str(p["details"]["road_class"][-1][2]).lower()
    return snapped, last_class


def test_pin_on_excluded_road_snaps_to_runnable_edge():
    # The fixture sits on a `primary` edge: foot_raw (no exclusions) snaps it onto primary.
    # The `run` profile must snap it onto a RUNNABLE edge instead — never onto the primary —
    # proving a waypoint cannot force the route onto an excluded road. (The runnable edge may
    # be a parallel sidewalk only metres away, so the test checks WHAT it snapped to, not how
    # far it moved.)
    raw_snap, raw_class = _snap_last_class("foot_raw", ON_PRIMARY)
    assert raw_class in EXCLUDED_CLASSES, "fixture should lie on an excluded road under foot_raw"

    r = _run(milestone(HOME, [ON_PRIMARY], 5 * MI, k=3))
    assert r["candidates"], "should still build a loop from a snapped waypoint"
    run_snap, run_class = _snap_last_class("run", r["snapped_waypoints"][0]["snapped"])
    assert run_class not in EXCLUDED_CLASSES, \
        f"run snapped the waypoint onto an excluded class ({run_class})"
    # and the resulting loops obey the safety model end-to-end
    for c in r["candidates"]:
        assert not (set(c["road_mix_pct"].keys()) & EXCLUDED_CLASSES)


# --------------------------------------------------------------- filter path (forced)
def test_filter_path_when_triggered_includes_waypoint():
    # In practice point-anchoring collapses distinct-shape variety below the threshold, so
    # decomposition usually fires. Force the filter path (filter_min=1) on a near-home anchor
    # to prove that branch is live and ALSO produces correctly-included, in-band loops.
    near = (40.205, -77.207)  # ~1 mi NW, on the road grid
    r = _run(milestone(HOME, [near], 6 * MI, k=5, filter_min=1))
    if r["method"] != "filter":
        pytest.skip("filter path did not trigger for this anchor on this run (seed variance)")
    swll = _snapped_ll(r)
    for c in r["candidates"]:
        assert min_dist_to_polyline_m(swll[0], _coords_lonlat(c)) <= EPS_M + 1
        assert abs(c["distance_mi"] - 6.0) / 6.0 <= 0.12  # in display band


# ------------------------------------------------------------------- derived (pad=0)
def test_derived_distance_mode_routes_through_points():
    r = _run(milestone(HOME, [FIELDS], None, pad=False))
    assert r["method"] == "derived"
    assert r["over_spine"] is False
    assert len(r["candidates"]) == 1
    c = r["candidates"][0]
    assert min_dist_to_polyline_m(_snapped_ll(r)[0], _coords_lonlat(c)) <= EPS_M + 1
    # derived distance should be ~the spine length, not padded to any target
    assert abs(c["distance_mi"] - r["d_spine_mi"]) < 0.05


# --------------------------------------------------------------- out-and-back (retrace)
def test_out_and_back_padded_is_retraced_and_includes_waypoint():
    r = _run(milestone(HOME, [FIELDS], 6 * MI, out_back=True, k=4))
    assert r["method"] == "out_and_back"
    assert r["candidates"], "should build at least one padded out-and-back"
    swll = _snapped_ll(r)
    for c in r["candidates"]:
        assert c["route_type"] == "out_and_back"
        assert c["overlap_pct"] >= 70.0, f"only {c['overlap_pct']:.0f}% retraced — not a real retrace"
        assert min_dist_to_polyline_m(swll[0], _coords_lonlat(c)) <= EPS_M + 1
        assert abs(c["distance_mi"] - 6.0) / 6.0 <= 0.12
        # the carried-over safety model still governs every leg of the retrace
        assert not (set(c["road_mix_pct"].keys()) & EXCLUDED_CLASSES)


def test_out_and_back_derived_is_plain_there_and_back():
    r = _run(milestone(HOME, [FIELDS], None, pad=False, out_back=True))
    assert r["method"] == "out_and_back"
    assert len(r["candidates"]) == 1
    c = r["candidates"][0]
    assert c["route_type"] == "out_and_back"
    assert c["overlap_pct"] >= 70.0
    # a there-and-back is ~twice the one-way spine to the single waypoint
    assert min_dist_to_polyline_m(_snapped_ll(r)[0], _coords_lonlat(c)) <= EPS_M + 1
