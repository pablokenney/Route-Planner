# Plan: Self-Hosted Running-Route Loop Generator (rev. 2)

> In-repo source of truth for the design. Phase 0 is being built first as a foundation
> probe. Do not build Phases 1–4 until Phase 0's milestone is hit and reviewed.

## Context

A free, self-hostable web tool that takes a **start address** (or saved start point) and a
**target distance** and generates runnable **loop routes** with an **elevation profile** and
**GPX export** — an open analog to Strava's premium route builder, including the
target-distance loop generation Strava locks behind premium.

Primary use case: **half-marathon training** from home in **Carlisle, PA — lat
40.195016, lon -77.199929 (confirmed exact)**. Need **flat-ish, low-traffic loops from 3
to 11 miles**. Free / open-source tooling only, no subscription or realistic quota.

**Hard requirements (non-negotiable):**
- Avoid highways entirely — no motorway/trunk, and exclude primary/secondary arterials.
- Avoid any road with a posted speed limit **over 25 mph**.
- Prefer residential, living streets, footways/paths, cycleways, park/trail segments.

**Core data caveat:** OSM `maxspeed` tags are incomplete. A naive `maxspeed <= 25` filter
both wrongly drops untagged residential streets and wrongly admits untagged fast roads.
Enforcement combines the `highway` road-class hierarchy (primary, reliable signal) with
`maxspeed` used **only to reject, never to admit** (§3).

### Locked decisions
- **Routing engine: GraphHopper**, run as a **plain Java JAR** (no Docker on this machine)
  — *provisional*, pending the Phase 0 composition probe. **OSMnx pre-filtered graph is the
  designated fallback** chosen by that probe (§2a).
- **Profile base: `foot`** (running).
- **Tertiary AND unclassified:** penalize heavily (last-resort); hard-exclude when tagged
  `maxspeed > 25`.
- **Flatness:** soft scoring factor only — not optimized. **SRTM 30m**; 3DEP deferred.
- **Surface:** UI preference, default "no preference."
- **Distance tolerance:** ±5–8%; no ±3% grind.
- **Candidates per request:** top **3–5**.
- **Stack:** Python (FastAPI) orchestrator + TypeScript/React+Leaflet frontend.
- **Tiles:** plain OSM raster to start.
- **Geocoding:** public Nominatim, cached.
- **Saved start points:** Phase 4.

## 2. Routing Engine: GraphHopper (provisional)

Differentiator vs Valhalla: GraphHopper's **native `round_trip` primitive** (Valhalla can
express the road/speed intent via pedestrian costing but lacks first-class round-trip).
OSRM compiles profiles at build time — bad for iteration.

Two verified facts:
1. `max_speed` is a `DecimalEncodedValue` that **rounds to a storable value via a scaling
   factor**, so `maxspeed=25 mph` (40.23 km/h) can be stored as high as ~45 km/h. Drives
   the **`>45` km/h** exclusion threshold and the read-back test.
2. `round_trip` honors a custom model **only in flex mode** (`ch.disable=true`). Under
   hybrid/LM a *per-request* custom model is restricted to `multiply_by ∈ [0,1]`, so all
   "prefer" multipliers (>1) live in the **server-side profile**, not per-request.

### 2a. Fallback: OSMnx pre-filtered NetworkX graph
If the Phase 0 probe shows `round_trip` does not honor the custom model cleanly, pivot to
pulling the walking network with OSMnx, dropping disallowed edges at construction time, and
running loop search over a graph that structurally cannot produce a violating route. The
pivot decision is the user's to make based on the Phase 0 report.

## 3. Enforcing "No Highways, ≤25 mph"

Server-side profile custom model (flex), rules in priority order:
1. Hard-exclude by class (`multiply_by: 0`): MOTORWAY, TRUNK, PRIMARY, SECONDARY + `_link`.
2. Hard-exclude by explicit speed: any edge with `max_speed > 45 km/h` (margin for rounding
   of 25 mph; still catches 30 mph → stored ~50).
3. Penalize TERTIARY and UNCLASSIFIED alike (~0.15); hard-exclude either when
   `max_speed > 45`.
4. SERVICE: allow, mild penalty (~0.7). TRACK: allow, surface-governed. ROAD (generic):
   penalize like tertiary, exclude if `max_speed > 45`.
5. Prefer (>1, server-side only): RESIDENTIAL ~1.0, LIVING_STREET ~1.3,
   FOOTWAY/PATH/CYCLEWAY/PEDESTRIAN ~1.2. Untagged residential kept (never gated on tag).
6. Access: exclude `road_access ∈ {PRIVATE, NO}`; respect foot access.

### Verification (`tests/route_rules`)
1. Highway rejection: zero {motorway,trunk,primary,secondary} edges.
2. Speed rejection: known `maxspeed>25` edge never appears.
3. **Explicit-25 admission:** read back stored `max_speed` for a known 25-mph street;
   assert admitted and stored value ≤ 45.
4. Untagged-residential admission.
5. Tertiary/unclassified last-resort only.

## 4. Loop Generation
GraphHopper `round_trip` + Python seed-iteration (N≈15–30 concurrent, varied seed) →
score (distance-dominant) → top 3–5. Fallback: round_trip-for-shape + post-filter
violating candidates (worsens distance accuracy; also a signal to pivot to OSMnx).
Realistic accuracy: best candidate ~±5–8%.

## 5. Data & Setup
Geofabrik Pennsylvania `.osm.pbf` clipped to a Carlisle bbox; SRTM 30m elevation;
GraphHopper import → graph cache; flex (no CH). Runs locally; Nominatim public+cached.

### 5a. Long-loop feasibility + trail audit
8–11 mi residential-only loops likely exhaust the network; the LeTort Spring Run trail +
borough paths are the long-run answer **iff** well-tagged and connected in OSM. Audit
runs in Phase 0/1 (`scripts/trail_audit.py` → `TRAIL_AUDIT.md`). Resolution for gaps:
prefer "lollipop" out-and-back on the trail over admitting fast tertiary/unclassified.

## 6. Phased Build Plan
- **Phase 0 (current):** composition probe + walking skeleton. Prove `round_trip` honors
  the exclusion model; one constraint-respecting loop on a map + GPX. STOP at milestone.
- **Phase 1:** full custom model + `tests/route_rules` + trail audit.
- **Phase 2:** real loop generator (seed-iteration + scoring).
- **Phase 3:** elevation profile + UI.
- **Phase 4:** surface preference + saved starts + caching.

## Critical files
- `scripts/run_graphhopper.sh` — launch GraphHopper JAR (no docker-compose on this machine).
- `graphhopper/config.yml`, `graphhopper/run-profile.json` — encoded values, flex, §3 model.
- `backend/` — `main.py`, `loop_generator.py`, `scoring.py`, `gpx.py`, `geocode.py`,
  `postfilter.py`.
- `frontend/` — Leaflet map, controls, elevation chart, candidate switcher, GPX download.
- `tests/route_rules/` — rule + composition + accuracy tests.
- `scripts/trail_audit.py` — LeTort/borough OSM coverage check (§5a).
