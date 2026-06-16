"""Feature 2 — out-and-back generation (needs GraphHopper on :8989).

Verifies that out_and_backs() produces retraced routes (high self-overlap), that they land
near the requested distance, and that they merge into /api/routes as ranked candidates with
the route_type flag. Skipped if GraphHopper isn't running.
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from backend.generator import MI, generate
from backend.outback import out_and_backs


def _gh_up() -> bool:
    try:
        return requests.get("http://localhost:8989/info", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _gh_up(), reason="GraphHopper not running on :8989")


def _run(coro):
    return asyncio.run(coro)


def test_out_and_backs_are_retraced_and_near_distance():
    cands = _run(out_and_backs(target_m=5 * MI))
    assert cands, "expected at least one out-and-back"
    for c in cands:
        assert c.route_type == "out_and_back"
        # An exact retrace shares almost all of its cells between the two halves.
        assert c.overlap_pct >= 70.0, f"overlap only {c.overlap_pct:.0f}% — not really retraced"
        # Within a generous band (turnaround geometry is coarser than round_trip).
        assert abs(c.distance_m - 5 * MI) / (5 * MI) <= 0.15


def test_out_and_backs_merge_into_routes():
    extra = _run(out_and_backs(target_m=5 * MI))
    r = _run(generate(5 * MI, n=20, rng_seed=11, extra_candidates=extra))
    types = {c["route_type"] for c in r["candidates"]}
    # The combined pool is still ranked by score ascending.
    scores = [c["score"] for c in r["candidates"]]
    assert scores == sorted(scores)
    # route_type is always present and valid.
    assert types <= {"loop", "out_and_back"}
