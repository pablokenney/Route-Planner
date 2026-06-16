#!/usr/bin/env bash
# Run the Phase 0 FastAPI backend (serves the frontend at http://localhost:8000).
# GraphHopper must already be running (scripts/run_graphhopper.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
