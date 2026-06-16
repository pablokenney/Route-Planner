# Route Planner

A free, self-hostable tool that generates runnable **loop routes** from a start point and a
target distance, avoiding highways and roads over 25 mph. See [`PLAN.md`](./PLAN.md) for the
full design.

> **Status: Phase 0** — foundation probe + walking skeleton. Proving that GraphHopper's
> `round_trip` honors the exclusion custom model before any feature work. Phases 1–4 are
> not built yet.

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

# Step 3 — composition probe (the gate); GraphHopper must be running
.venv/bin/python tests/route_rules/test_composition_probe.py   # human report
.venv/bin/python -m pytest tests/route_rules/ -v                # assertions
```

## Layout
| Path | Purpose |
|---|---|
| `graphhopper/config.yml` | GraphHopper config — `run` profile (flex, foot base, SRTM) + diagnostic `foot_raw` |
| `graphhopper/run-profile.json` | Exclusion custom model (no-highway / ≤25 mph) |
| `scripts/fetch_data.sh` | Download JAR + PA extract, clip to Carlisle bbox |
| `scripts/run_graphhopper.sh` | Launch GraphHopper JAR |
| `scripts/trail_audit.py` | LeTort/borough trail OSM coverage audit → `TRAIL_AUDIT.md` |
| `backend/main.py` | FastAPI: `/api/route`, `/api/gpx` |
| `frontend/index.html` | Leaflet map + GPX download |
| `tests/route_rules/` | Composition probe + rule tests |
