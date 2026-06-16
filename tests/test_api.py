"""Phase 4 — GraphHopper-independent API tests: saved starts (CRUD + upsert), surface
validation, and the routes result-cache key. The generation endpoints themselves need GH
and are covered by tests/test_generator.py.
"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    """App with STARTS_FILE redirected into a temp dir so tests never touch real data."""
    import backend.main as m
    importlib.reload(m)
    m.STARTS_FILE = os.path.join(tmp_path, "starts.json")
    return TestClient(m.app)


def test_starts_empty_by_default(client):
    assert client.get("/api/starts").json() == []


def test_starts_save_list_sorted(client):
    client.post("/api/starts", json={"name": "Zephyr", "lat": 40.2, "lon": -77.1})
    client.post("/api/starts", json={"name": "Alpha", "lat": 40.3, "lon": -77.2})
    names = [s["name"] for s in client.get("/api/starts").json()]
    assert names == ["Alpha", "Zephyr"]  # case-insensitive name sort


def test_starts_upsert_by_name(client):
    client.post("/api/starts", json={"name": "Home", "lat": 1.0, "lon": 2.0})
    client.post("/api/starts", json={"name": "Home", "lat": 9.0, "lon": 8.0})
    starts = client.get("/api/starts").json()
    assert len(starts) == 1
    assert starts[0]["lat"] == 9.0 and starts[0]["lon"] == 8.0


def test_starts_validation(client):
    assert client.post("/api/starts", json={"lat": 1, "lon": 2}).status_code == 422
    assert client.post("/api/starts", json={"name": "x", "lat": "nope", "lon": 2}).status_code == 422
    assert client.post("/api/starts", json={"name": "x", "lat": 999, "lon": 2}).status_code == 422


def test_starts_delete(client):
    client.post("/api/starts", json={"name": "A", "lat": 1, "lon": 2})
    assert client.delete("/api/starts/A").status_code == 200
    assert client.get("/api/starts").json() == []
    assert client.delete("/api/starts/A").status_code == 404  # already gone


def test_routes_rejects_bad_surface(client):
    # surface validation happens before any GraphHopper call, so this is safe without GH
    assert client.get("/api/routes?surface=gravel").status_code == 422
    assert client.get("/api/gpx?surface=gravel").status_code == 422


def test_cache_key_rounds_nearby_coords_together():
    from backend.main import _cache_key
    a = _cache_key(5.0, 40.195016, -77.199929, 25, 0.08, 5, "paved")
    b = _cache_key(5.001, 40.1950161, -77.1999291, 25, 0.08, 5, "paved")
    assert a == b  # same place, ~11 m rounding -> shared cache entry
    assert a != _cache_key(5.0, 40.195016, -77.199929, 25, 0.08, 5, "any")  # surface matters
