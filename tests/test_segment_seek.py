"""Auto-seek: loops through nearby known segments are constructed and fold into the pool.

The round_trip generator never targets a specific trail (a heading hint toward one often
fails to close a loop at all), so segment_loops() builds loops THROUGH nearby segments via the
milestone decomposition. This test asserts that, from HOME at 8.5 mi, at least one constructed
loop genuinely covers the LeTort trail segment — and that those loops survive into a ranked
result when folded as extra_candidates. Skipped if GraphHopper isn't running.
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from backend.generator import MI, generate
from backend.segment_seek import SPUR_TIPS, segment_loops, spur_loops
from backend.segments import _SEGMENTS, segments_on_route


def _gh_up() -> bool:
    try:
        return requests.get("http://localhost:8989/info", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _gh_up(), reason="GraphHopper not running on :8989")

HOME = (40.195016, -77.199929)


def _run(coro):
    return asyncio.run(coro)


def _has_letort() -> bool:
    return any("letort" in s["name"].lower() for s in _SEGMENTS)


@pytest.mark.skipif(not _has_letort(), reason="LeTort segment not in segments.json")
def test_segment_loops_construct_a_letort_loop():
    built = _run(segment_loops(HOME, 8.5 * MI, surface="any"))
    assert built, "expected constructed loops through nearby segments"
    # at least one constructed loop actually covers the LeTort trail segment
    covers = [c for c in built
              if any("letort" in s["name"].lower() for s in segments_on_route(c.coords))]
    assert covers, "no constructed loop covered the LeTort trail segment"
    # constructed loops route on profile=run and target the requested distance
    for c in covers:
        assert abs(c.distance_m - 8.5 * MI) / (8.5 * MI) <= 0.20


@pytest.mark.skipif(not _has_letort(), reason="LeTort segment not in segments.json")
def test_seeked_segment_loops_survive_ranking():
    # fold the constructed loops in exactly as /api/routes does; a LeTort loop should rank in.
    extra = _run(segment_loops(HOME, 8.5 * MI, surface="any"))
    d = _run(generate(8.5 * MI, n=30, start=HOME, surface="any",
                      extra_candidates=extra, rng_seed=1))
    on_letort = [c for c in d["candidates"]
                 if any("letort" in s["name"].lower() for s in c["segments"])]
    assert on_letort, "no LeTort-covering loop surfaced in the ranked 8.5 mi results"


def test_segment_loops_empty_when_out_of_range():
    # a 0.6 mi loop can't reach any segment far from HOME -> graceful empty (no crash)
    built = _run(segment_loops((0.0, 0.0), 0.6 * MI, surface="any"))
    assert built == []


# ----------------------------------------------------------------- dead-end trail-tip spurs
def test_spur_loops_reach_a_dead_end_tip_as_a_retrace():
    # The LeTort south end is a dead-end (its only onward road is the blocked Heisers Lane), so a
    # loop never reaches it; spur_loops builds a down-and-back to it, tagged as a retrace.
    built = _run(spur_loops(HOME, 8.5 * MI, surface="any"))
    assert built, "expected a constructed spur to the curated trail tip"
    tip = SPUR_TIPS[0]
    for c in built:
        assert c.route_type == "out_and_back", "a dead-end spur is a retrace; must be tagged so"
        assert c.overlap_pct > 0, "retrace overlap should be reported"
        assert abs(c.distance_m - 8.5 * MI) / (8.5 * MI) <= 0.20
        # the route actually reaches the curated tip (within a block)
        near_tip = min(_hav_m([p[0], p[1]], [tip["lon"], tip["lat"]]) for p in c.coords)
        assert near_tip <= 60, f"spur never reached the tip (closest {near_tip:.0f} m)"


def test_spur_loops_empty_when_tip_out_of_range():
    # a 0.6 mi loop can't reach the LeTort tip from HOME -> graceful empty (no crash)
    assert _run(spur_loops(HOME, 0.6 * MI, surface="any")) == []


def _hav_m(a, b):
    import math
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[1], a[0], b[1], b[0]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
