# Route Planner

A free, self-hostable tool that generates runnable **loop routes** from a start point and a
target distance, avoiding highways and roads over 25 mph. See [`PLAN.md`](./PLAN.md) for the
full design.

> **Status: Phase 3 complete** — the first real UI. Open **http://localhost:8000**: enter
> a start address (or use home), pick a distance (presets 3/5/8/11 mi or custom), and get
> ranked in-band candidate loops as selectable cards (distance, elevation gain, road-mix);
> selecting one updates the map, the elevation profile chart, and the GPX download. Phases
> 0–2 (rules, reachability, generator) are below. Phase 4 (surface preference, saved start
> points, caching) is not built yet.

## API
- `GET /api/routes?miles=5&lat=&lon=&n=25&tolerance=0.08&k=5` → ranked, **in-band**,
  de-duplicated candidates (each with geometry, `elevations`, actual distance, gain,
  road-mix %, score breakdown). Returns fewer than `k` rather than padding with
  out-of-band loops; `shortfall: true` + `message` when nothing hits tolerance.
  Display band: **±12%** of target (slightly wider than the ±8% tolerance).
- `GET /api/route?distance_m=8047&lat=&lon=` → the single best candidate.
- `GET /api/gpx?distance_m=<gen_distance_m>&seed=<seed>&lat=&lon=` → GPX for a chosen loop.
- `GET /api/geocode?q=<address>` → `{lat, lon, display_name}` via Nominatim (disk-cached).

## Stack (Phase 0)
- **GraphHopper 10.2** as a plain Java JAR (no Docker) — routing + `round_trip` loops.
- **FastAPI** backend — proxies one round_trip, exports GPX.
- **Leaflet** frontend — draws the loop, downloads GPX.
- **OSMnx** — one-off trail-coverage audit (`scripts/trail_audit.py`).

## Setup
```bash
# 1. Toolchain (already scripted): JDK + geo libs
brew install openjdk geos gdal spatialindex osmium-tool
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt

# 2. Data + GraphHopper JAR (downloads PA extract, clips to Carlisle bbox)
scripts/fetch_data.sh

# 3. Import + run GraphHopper (first run imports the graph; ~minutes)
scripts/run_graphhopper.sh            # serves on :8989

# 4. (separate shell) run the backend + UI
scripts/run_backend.sh                # http://localhost:8000
```

## Phase 0 verification
```bash
# Step 1 — trail audit (writes TRAIL_AUDIT.md)
.venv/bin/python scripts/trail_audit.py

# Composition probe (Phase 0 gate); GraphHopper must be running
.venv/bin/python tests/route_rules/test_composition_probe.py   # human report

# Full rule-enforcement suite (Phase 1) — 10 tests
.venv/bin/python -m pytest tests/route_rules/ -v

# Reachability baseline (Phase 1) -> REACHABILITY.md
.venv/bin/python scripts/reachability.py
```

## Layout
| Path | Purpose |
|---|---|
| `graphhopper/config.yml` | GraphHopper config — `run` profile (flex, foot base, SRTM) + diagnostic `foot_raw` / `run_noprefs` |
| `graphhopper/run-profile.json` | Full `run` custom model — exclusions (no-highway / ≤25 mph) + preferences |
| `graphhopper/run-noprefs.json` | Exclusions-only model (baseline for the preference-sanity test) |
| `scripts/reachability.py` | Reachability baseline → `REACHABILITY.md` |
| `scripts/fetch_data.sh` | Download JAR + PA extract, clip to Carlisle bbox |
| `scripts/run_graphhopper.sh` | Launch GraphHopper JAR |
| `scripts/trail_audit.py` | LeTort/borough trail OSM coverage audit → `TRAIL_AUDIT.md` |
| `backend/main.py` | FastAPI: `/api/route`, `/api/gpx` |
| `frontend/index.html` | Leaflet map + GPX download |
| `tests/route_rules/` | Composition probe + rule tests |
