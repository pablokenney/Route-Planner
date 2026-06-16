"""Phase 4 — surface-preference model + request-body construction.

These are pure-logic tests (no GraphHopper): they assert the per-request custom model is
shaped correctly and, critically, that it can NEVER un-exclude an edge the server-side `run`
profile zeroed (all surface multipliers are > 0, so they only re-weight already-allowed
edges). They also pin the GET->POST round_trip body so GPX reproduction stays faithful.
"""
from __future__ import annotations

import pytest

from backend.generator import (
    SURFACE_CHOICES,
    _SURFACE_PAVED,
    _SURFACE_UNPAVED,
    round_trip_body,
    surface_custom_model,
)


def test_any_means_no_model():
    assert surface_custom_model("any") is None
    assert surface_custom_model("") is None
    assert surface_custom_model(None) is None


def test_unknown_surface_rejected():
    with pytest.raises(ValueError):
        surface_custom_model("cobblestone")


@pytest.mark.parametrize("surface", ["paved", "unpaved", "PAVED", "Unpaved"])
def test_model_only_reweights_never_excludes(surface):
    # Every multiplier is strictly > 0: a surface preference can demote but never zero out
    # (un-include) an edge, so the no-highway / >45 km/h hard-excludes remain absolute.
    model = surface_custom_model(surface)
    assert "priority" in model and model["priority"]
    for rule in model["priority"]:
        assert float(rule["multiply_by"]) > 0.0
        assert rule["if"].startswith("surface == ")


def test_paved_prefers_paved_demotes_unpaved():
    rules = {r["if"]: float(r["multiply_by"]) for r in surface_custom_model("paved")["priority"]}
    assert rules[f"surface == {_SURFACE_PAVED[0]}"] > 1.0
    assert rules[f"surface == {_SURFACE_UNPAVED[0]}"] < 1.0


def test_unpaved_is_mirror_of_paved():
    paved = {r["if"]: float(r["multiply_by"]) for r in surface_custom_model("paved")["priority"]}
    unpaved = {r["if"]: float(r["multiply_by"]) for r in surface_custom_model("unpaved")["priority"]}
    # the asphalt rule flips from prefer to demote between the two
    asphalt = f"surface == {_SURFACE_PAVED[0]}"
    assert paved[asphalt] > 1.0 and unpaved[asphalt] < 1.0


def test_surface_choices_advertised():
    assert SURFACE_CHOICES == ("any", "paved", "unpaved")


def test_round_trip_body_uses_lon_lat_and_carries_model():
    start = (40.195016, -77.199929)  # (lat, lon)
    body = round_trip_body(seed=42, gen_distance_m=8000, start=start,
                           custom_model=surface_custom_model("paved"))
    # GH POST expects [lon, lat]
    assert body["points"] == [[-77.199929, 40.195016]]
    assert body["profile"] == "run"
    assert body["algorithm"] == "round_trip"
    assert body["round_trip.distance"] == 8000
    assert body["round_trip.seed"] == 42
    assert body["ch.disable"] is True
    assert "custom_model" in body


def test_round_trip_body_omits_model_when_none():
    body = round_trip_body(1, 5000, (40.1, -77.1), None)
    assert "custom_model" not in body
