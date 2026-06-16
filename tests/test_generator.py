"""Phase 2 — loop generator behavior tests.

Stochastic (random seeds), so distance assertions use the agreed ±8% tolerance with a
little slack and run a few trials rather than asserting a single lucky pull. Skipped if
GraphHopper isn't running.
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from backend.generator import MI, _cells, _jaccard, expected_lastresort_frac, generate


def _gh_up() -> bool:
    try:
        return requests.get("http://localhost:8989/info", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _gh_up(), reason="GraphHopper not running on :8989")


def _run(coro):
    return asyncio.run(coro)


def test_distance_within_tolerance_typical():
    # best candidate should land within tolerance for an easy distance, most of the time
    hits = 0
    for t in range(4):
        r = _run(generate(5 * MI, n=25, tolerance=0.08, rng_seed=100 + t))
        best_err = abs(r["candidates"][0]["distance_mi"] - 5.0) / 5.0
        if best_err <= 0.085:
            hits += 1
    assert hits >= 3, f"best candidate within ~8% only {hits}/4 runs"


def test_returns_ranked_distinct_candidates():
    r = _run(generate(8 * MI, n=25, rng_seed=7))
    cands = r["candidates"]
    assert 1 <= len(cands) <= 5
    # ranked by score ascending
    scores = [c["score"] for c in cands]
    assert scores == sorted(scores)
    # de-dup: no two returned loops are near-identical
    cellsets = [_cells([(ll[1], ll[0]) for ll in c["latlngs"]]) for c in cands]
    for i in range(len(cellsets)):
        for j in range(i + 1, len(cellsets)):
            assert _jaccard(cellsets[i], cellsets[j]) <= 0.6, "returned loops too similar"


def test_distance_dominates_ranking():
    # rank-1 should be (near) the closest-distance candidate, not a farther "cleaner" one
    r = _run(generate(5 * MI, n=25, rng_seed=3))
    dists = [c["distance_mi"] for c in r["candidates"]]
    closest = min(dists, key=lambda d: abs(d - 5.0))
    assert abs(dists[0] - closest) < 1e-6, "rank-1 is not the closest-distance candidate"


def test_shortfall_is_honest():
    r = _run(generate(11 * MI, n=25, tolerance=0.0, rng_seed=1))  # exact match is impossible
    assert r["shortfall"] is True
    assert r["message"] and "closest I could get" in r["message"]
    assert r["candidates"], "shortfall must still return the closest achievable loops"


def test_distance_aware_expectation_scales():
    # the road-mix expectation must grow with distance so long loops aren't over-penalized
    assert expected_lastresort_frac(3) < expected_lastresort_frac(8) < expected_lastresort_frac(11)
    assert expected_lastresort_frac(1) == pytest.approx(0.05)   # clamped low
    assert expected_lastresort_frac(20) == pytest.approx(0.25)  # clamped high
