"""Phase 5 — milestone geometry helpers (GraphHopper-independent).

Pins down the pure math the milestone filter relies on: point-to-polyline distance (the ε
test) and the bearing hint. These run without a live engine so the core inclusion logic is
verifiable in isolation from routing.
"""
from __future__ import annotations

import math

from backend.milestone import (
    EPS_M,
    _initial_bearing,
    min_dist_to_polyline_m,
    passes_through,
)

# A short polyline in [lon, lat] order (GraphHopper convention), near home.
LINE = [[-77.20, 40.195], [-77.19, 40.195], [-77.18, 40.195]]


def test_point_on_line_is_zero_distance():
    assert min_dist_to_polyline_m([-77.19, 40.195], LINE) < 1.0


def test_point_to_segment_not_just_vertices():
    # A point above the MIDDLE of the first segment (not near any vertex) must measure the
    # perpendicular offset, not the distance to the nearest endpoint.
    d = min_dist_to_polyline_m([-77.195, 40.196], LINE)
    assert 100 < d < 130, d  # ~111 m per 0.001 deg lat


def test_passes_through_requires_every_waypoint():
    on = [-77.19, 40.195]        # on the line
    off = [-77.19, 40.20]        # ~550 m north, off the line
    assert passes_through(LINE, [on], eps_m=EPS_M)
    assert not passes_through(LINE, [off], eps_m=EPS_M)
    # ALL must pass — one good + one bad => fails (never a silent drop)
    assert not passes_through(LINE, [on, off], eps_m=EPS_M)


def test_bearing_cardinals():
    # bearing is degrees clockwise from north, in [lon,lat]
    assert abs(_initial_bearing([0, 0], [0, 1]) - 0) < 1      # due north
    assert abs(_initial_bearing([0, 0], [1, 0]) - 90) < 1     # due east
    assert abs(_initial_bearing([0, 0], [0, -1]) - 180) < 1   # due south
    assert abs(_initial_bearing([0, 0], [-1, 0]) - 270) < 1   # due west


def test_eps_default_is_documented_value():
    # The chosen ε (PLAN.md §Phase 5). Guards against an accidental silent change.
    assert EPS_M == 40.0
