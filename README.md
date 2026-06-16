# Route Planner

A free, self-hostable tool that generates runnable **loop routes** from a start point and a
target distance, avoiding highways and roads over 25 mph. See [`PLAN.md`](./PLAN.md) for the
full design.

> **Status: Phase 5 complete** — Milestone Mode added. Open **http://localhost:8000**: enter
> a start address (or use home / a saved start), pick a distance (presets 3/5/8/11 mi or
> custom) and a **surface preference** (any / paved / unpaved), and get ranked in-band
> candidate loops as selectable cards (distance, elevation gain, road-mix); selecting one
> updates the map, the elevation profile chart, and the GPX download. Start points can be
> **saved** (named, server-side) and re-selected; generation results are **cached**.
>
> **Milestone Mode** (toggle at top of the sidebar): drop pins on the map (or type
> addresses) for points the loop MUST pass through, then pad out to a target distance —
> "anchor what I know, generate what I don't." Every leg still runs the `run` profile, so
> the no-highway / ≤25 mph guarantee carries over; a pin on an excluded road snaps to the
> nearest runnable edge. `Pad to target` pads up to the chosen distance; `Exact (derived)`
> just routes through the points. Milestones can be saved. Phases 0–4 are below.

## API
- `GET /api/routes?miles=5&lat=&lon=&n=25&tolerance=0.08&k=5&surface=any&fresh=false` →
  ranked, **in-band**, de-duplicated candidates (each with geometry, `elevations`, actual
  distance, gain, road-mix %, score breakdown). Returns fewer than `k` rather than padding
  with out-of-band loops; `shortfall: true` + `message` when nothing hits tolerance.
  Display band: **±12%** of target (slightly wider than the ±8% tolerance).
  `surface` ∈ {`any`,`paved`,`unpaved`} is a **soft** preference (never relaxes the
  no-highway / ≤25 mph hard rules). Results are cached ~15 min; `fresh=true` bypasses the
  cache and the response carries `cached: true|false`.
- `GET /api/route?distance_m=8047&lat=&lon=&surface=any` → the single best candidate.
- `GET /api/gpx?distance_m=<gen_distance_m>&seed=<seed>&lat=&lon=&surface=any` → GPX for a
  chosen loop. Pass the candidate's `surface` so the reproduced geometry matches the card.
- `GET /api/geocode?q=<address>` → `{lat, lon, display_name}` via Nominatim (disk-cached).
- `GET /api/starts` · `POST /api/starts {name,lat,lon}` (upsert by name) ·
  `DELETE /api/starts/{name}` → saved start points, persisted to `starts.json`.
- `POST /api/milestone {waypoints:[{lat,lon}], lat, lon, miles, surface, pad}` → loops
  through **every** waypoint, padded to `miles` (or derived distance when `pad=false`).
  Returns the core candidate shape plus `method` (filter/decompose/derived/over_spine),
  snapped waypoints (with a `moved` flag), `d_spine_mi`, and the honest `over_spine` flag
  when the points are already farther apart than the target.
- `GET /api/milestones` · `POST /api/milestones {name,waypoints,miles?}` (upsert) ·
  `DELETE /api/milestones/{name}` → saved milestones, persisted to `milestones.json`.
- `POST /api/gpx_track {latlngs, elevations, name}` → GPX built directly from geometry
  (used for milestone candidates, incl. decomposed composites a round_trip can't reproduce).

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
| `backend/generator.py` | Loop generator + surface custom-model builder (`surface_custom_model`) |
| `backend/milestone.py` | Phase 5 milestone mode (filter + decomposition + over-spine) on top of the generator |
| `backend/main.py` | FastAPI: routes/route/gpx/geocode + saved starts + result cache + milestone + saved milestones |
| `frontend/index.html` | Leaflet map, candidate cards, elevation chart, surface + saved-starts UI, GPX download |
| `tests/route_rules/` | Composition probe + rule tests |
