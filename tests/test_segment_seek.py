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
from backend.segment_seek import segment_loops
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
