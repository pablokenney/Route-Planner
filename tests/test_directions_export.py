"""Features 1 & 4 — turn synthesis, GH instruction parsing, and TCX export (no GraphHopper).

Pure-logic coverage: parse_instructions on a synthetic GH path, synthesize_turns bend
detection on hand-built geometry, and the TCX builder's distance-tagged course points.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from backend.generator import parse_instructions, synthesize_turns
from backend.main import _tcx_xml


def test_parse_instructions_cumulative():
    path = {"instructions": [
        {"text": "Head north", "sign": 0, "distance": 100.0, "interval": [0, 2]},
        {"text": "Turn right", "sign": 2, "distance": 50.0, "interval": [2, 4]},
        {"text": "Arrive", "sign": 4, "distance": 0.0, "interval": [4, 4]},
    ]}
    turns = parse_instructions(path)
    assert [t["cumulative_m"] for t in turns] == [0.0, 100.0, 150.0]
    assert turns[1]["text"] == "Turn right" and turns[1]["sign"] == 2


def test_synthesize_turns_detects_right_angle():
    # An L-shaped path: east for ~300 m, then a sharp left (north) for ~300 m.
    coords = [[-77.20, 40.20], [-77.197, 40.20], [-77.197, 40.203]]
    turns = synthesize_turns(coords)
    signs = [t["sign"] for t in turns]
    assert turns[0]["text"] == "Head out"
    assert turns[-1]["sign"] == 4  # arrival
    assert any(s in (-2, -1) for s in signs), "should detect a left turn at the corner"
    # cumulative distance is monotonic
    cums = [t["cumulative_m"] for t in turns]
    assert cums == sorted(cums)


def test_synthesize_turns_straight_line_has_no_turns():
    coords = [[-77.20, 40.20], [-77.198, 40.20], [-77.196, 40.20]]
    turns = synthesize_turns(coords)
    # Only the head-out and arrival cues, no mid-route turns.
    assert [t["sign"] for t in turns] == [0, 4]


def test_tcx_has_trackpoints_and_coursepoints():
    latlngs = [[40.20, -77.20], [40.20, -77.198], [40.203, -77.198]]
    elevations = [120.0, 121.0, 125.0]
    turns = [
        {"text": "Head out", "sign": 0, "cumulative_m": 0.0},
        {"text": "Turn left", "sign": -2, "cumulative_m": 170.0},
        {"text": "Arrive", "sign": 4, "cumulative_m": 500.0},
    ]
    xml = _tcx_xml(latlngs, elevations, turns, "Test Route")
    root = ET.fromstring(xml)
    ns = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}
    tps = root.findall(".//t:Trackpoint", ns)
    cps = root.findall(".//t:CoursePoint", ns)
    assert len(tps) == 3
    assert len(cps) == 3
    # Trackpoints carry monotonic DistanceMeters.
    dists = [float(tp.find("t:DistanceMeters", ns).text) for tp in tps]
    assert dists == sorted(dists) and dists[0] == 0.0
    # The left turn maps to PointType Left.
    ptypes = [cp.find("t:PointType", ns).text for cp in cps]
    assert "Left" in ptypes
