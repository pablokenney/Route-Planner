"""Feature 3 — Strava-segment detection + bias scoring (no GraphHopper needed).

Uses a synthetic segment fixture so the tests don't depend on scraped data. Verifies that
(a) a route covering a known segment is detected and one bypassing it is not, and (b) the
W_SEG bonus pulls a segment-covering candidate ahead of an otherwise-identical bypass.
"""
from __future__ import annotations

from backend import segments as seg
from backend.generator import MI, Candidate, score_candidate


# A short east-west segment near the Carlisle home, as [lat, lon] (segments.json shape).
_FIXTURE = [{
    "id": 999, "name": "Test Straight", "distance_m": 200.0,
    "latlngs": [[40.2000, -77.2000], [40.2000, -77.1980], [40.2000, -77.1960]],
}]
_SEGS = seg._prepare(_FIXTURE)


def _coords_along_segment():
    # A route polyline (lon, lat) that runs right along the fixture segment.
    return [[-77.2000, 40.2000], [-77.1985, 40.2000], [-77.1970, 40.2000], [-77.1960, 40.2000]]


def _coords_elsewhere():
    # A parallel route a few hundred metres north — should NOT match.
    return [[-77.2000, 40.2050], [-77.1980, 40.2050], [-77.1960, 40.2050]]


def test_segment_detected_on_overlapping_route():
    matched = seg.segments_on_route(_coords_along_segment(), segments=_SEGS)
    assert len(matched) == 1
    assert matched[0]["id"] == 999


def test_segment_not_detected_on_bypassing_route():
    assert seg.segments_on_route(_coords_elsewhere(), segments=_SEGS) == []


def test_empty_cache_is_inert():
    # With no segments, matching returns nothing and scoring is unchanged (back-compat).
    assert seg.segments_on_route(_coords_along_segment(), segments=[]) == []


def _candidate(coords, segs):
    c = Candidate(seed=1, gen_distance_m=5000, distance_m=5000.0, ascend_m=50.0,
                  descend_m=50.0, coords=coords)
    c.length_by_class = {"residential": 5000.0}
    c.segments = segs
    return c


def test_segment_bonus_improves_score():
    target = 5000.0
    # Two candidates identical except one is tagged with a covered segment.
    with_seg = _candidate(_coords_along_segment(),
                          [{"id": 999, "name": "Test", "distance_m": 200.0, "covered_m": 200.0}])
    without = _candidate(_coords_along_segment(), [])
    score_candidate(with_seg, target, target / MI)
    score_candidate(without, target, target / MI)
    # MINIMIZED score: the segment-covering candidate must score LOWER (better).
    assert with_seg.score < without.score
    assert with_seg.breakdown["segment_bonus"] > 0
    assert without.breakdown["segment_bonus"] == 0
