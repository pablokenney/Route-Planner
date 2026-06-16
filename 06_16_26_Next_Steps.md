# Next steps — status (updated 2026-06-16)

1. ~~Provide step by step breakdown of routes~~ ✅ DONE — GraphHopper turn instructions
   surfaced in a Directions panel (`/api/directions`) and embedded in exports. Composites
   (out-and-back, milestone) use geometry bend-detection (`/api/directions_track`).
2. ~~Allow for pure out and back routes, not necessitating a full loop~~ ✅ DONE —
   `backend/outback.py` generates retraced out-and-backs, ranked in the same candidate list
   as loops, each flagged with `route_type` + `overlap_pct` (≈100% retraced).
3. ~~Bias towards segments~~ ✅ DONE — geometry pulled from Strava's public stream endpoint
   (`https://www.strava.com/stream/segments/{id}`, returns `latlng` JSON, no auth) via
   `scripts/fetch_segments.py` → `data/segments.json` (99/100 resolved). `backend/segments.py`
   detects traversed segments; a `W_SEG` bonus nudges segment-rich routes up the ranking.

## Bonus — Apple Watch export ✅ DONE
TCX course export (`/api/tcx_track`) with distance-tagged CoursePoints for on-watch
turn-by-turn. Import the `.tcx` into WorkOutDoors / Footpath / WristTopo. Apple/Google Maps
deep-links were deliberately NOT added — they re-route along their own engine and won't
follow an arbitrary trail/loop path. GPX export retained.

## To refresh segment geometry
`python scripts/fetch_segments.py`  (re-run after editing Segments.csv; failures logged to
`data/segments_failures.json`). 1 segment (`9432162 AT from Lisburn to York`) is currently
unresolved — likely private/removed.
