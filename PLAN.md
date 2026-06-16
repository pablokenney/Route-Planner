# Plan: Self-Hosted Running-Route Loop Generator (rev. 2)

> In-repo source of truth for the design. **Phases 0–5 are complete** — see the per-phase
> status in §6. Phase 0 was built first as a foundation probe (GraphHopper GO). Phase 5.1
> (segment-anchoring) is the next planned step and is NOT yet built.

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
- **Phase 0 (done):** composition probe + walking skeleton. Prove `round_trip` honors
  the exclusion model; one constraint-respecting loop on a map + GPX. STOP at milestone.
- **Phase 1 (done):** full custom model + `tests/route_rules` + trail audit.
- **Phase 2 (done):** real loop generator (seed-iteration + scoring).
- **Phase 3 (done):** elevation profile + UI.
- **Phase 4 (done):** surface preference + saved starts + caching.
  - *Surface:* soft, default "any". Implemented as a **per-request custom model**
    (`backend/generator.surface_custom_model`) — legal under pure flex, where `multiply_by
    > 1` is allowed per-request and GH merges it on top of the `run` exclusions, so the
    no-highway / ≤25 mph hard rules stay absolute. Gentle strength (prefer ×1.4, demote
    ×0.6); untagged surfaces stay neutral. Threaded through generation AND GPX reproduction
    (round_trip switched GET→POST so the model rides in the body; GPX must reuse the same
    model or geometry drifts from the chosen candidate).
  - *Saved starts:* server-side `starts.json` (upsert by name), `GET/POST /api/starts`,
    `DELETE /api/starts/{name}`.
  - *Caching:* in-memory TTL cache on `/api/routes` keyed on the candidate-set inputs
    (start rounded to ~11 m, distance, surface, n, tol, k); `fresh=true` bypasses.
- **Phase 5 (done):** Milestone Mode — exact waypoints, padded distance. Built strictly ON
  TOP of the locked engine (`backend/milestone.py` reuses the generator's `_fire`,
  `_make_candidate`, `score_candidate`, `_serialize`, de-dup; no rule/multiplier/round_trip
  change). Every leg routes on `profile=run`, so the full safety model governs all of it.
  - *Two mechanisms, chosen by yield:* FILTER (primary) fires the unchanged round_trip
    fan-out at the target, biased toward the first waypoint via `headings`, and keeps loops
    passing within ε of every waypoint. DECOMPOSE (fallback) guarantees inclusion by
    construction: spine start→wp₁→…→wp_L, a round_trip anchored at wp_L sized to the
    remaining budget, then the spine reversed; pad-loop seed variation recovers variety
    (round_trip cannot combine with GH's alternative_route algorithm).
  - *Trigger (tunable):* `FILTER_MIN_CANDIDATES = 3` DISTINCT in-tolerance filtered loops to
    prefer the filter; below that → decompose. *ε = 40 m* (`EPS_M`) — a waypoint snaps onto a
    runnable edge, and a loop traversing that edge passes within metres.
  - *Empirical finding:* for single-point anchors the filter almost always yields 0–1
    *distinct* loops (forcing a loop through a fixed point collapses shape variety), so
    decomposition fires in practice. It still delivers ~5 varied loops at ~1.9% mean
    distance error (Dickinson fields → 6 mi, 8 runs, 100% waypoint inclusion).
  - *Snapping:* a pin on an excluded road snaps to the nearest RUNNABLE edge (never the
    excluded one) — proven by run-vs-foot_raw snapping in the tests. Reported to the UI.
  - *Over-spine (the one hard failure):* D_spine > target·(1+tol) is unsatisfiable (can't
    pad down); reported honestly, never by dropping a waypoint.
  - *Modes:* `pad=true` pads to target; `pad=false` → exact-waypoints, derived-distance
    (the spine itself). Saved milestones persist to `milestones.json` (Phase 4 pattern).
  - *NOT built (Phase 5.1):* segment-anchoring (forcing a known sub-route/edge sequence).

## Critical files
- `scripts/run_graphhopper.sh` — launch GraphHopper JAR (no docker-compose on this machine).
- `graphhopper/config.yml`, `graphhopper/run-profile.json` — encoded values, flex, §3 model.
- `backend/` — `main.py`, `generator.py`, `milestone.py` (Phase 5).
- `frontend/` — Leaflet map, controls, elevation chart, candidate switcher, GPX download.
- `tests/route_rules/` — rule + composition + accuracy tests.
- `scripts/trail_audit.py` — LeTort/borough OSM coverage check (§5a).
