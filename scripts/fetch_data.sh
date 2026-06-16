#!/usr/bin/env bash
# Fetch + clip OSM data for the Carlisle, PA bbox, and download the GraphHopper JAR.
# Idempotent: skips downloads that already exist.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/data"
GH="$ROOT/graphhopper"
mkdir -p "$DATA" "$GH"

GH_VERSION="10.2"
JAR="$GH/graphhopper-web-${GH_VERSION}.jar"
JAR_URL="https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/${GH_VERSION}/graphhopper-web-${GH_VERSION}.jar"

PA_PBF="$DATA/pennsylvania-latest.osm.pbf"
PA_URL="https://download.geofabrik.de/north-america/us/pennsylvania-latest.osm.pbf"

# Carlisle bbox: center 40.195016,-77.199929, ~5 mi each direction.
# osmium order: left,bottom,right,top  (W,S,E,N)
BBOX="-77.31,40.12,-77.09,40.27"
CARLISLE="$DATA/carlisle.osm.pbf"

echo "==> GraphHopper JAR (${GH_VERSION})"
if [[ ! -f "$JAR" ]]; then
  curl -fSL "$JAR_URL" -o "$JAR"
else
  echo "    exists, skipping"
fi

echo "==> Pennsylvania extract (Geofabrik)"
if [[ ! -f "$PA_PBF" ]]; then
  curl -fSL "$PA_URL" -o "$PA_PBF"
else
  echo "    exists, skipping"
fi

echo "==> Clip to Carlisle bbox -> carlisle.osm.pbf"
if [[ ! -f "$CARLISLE" ]]; then
  osmium extract -b "$BBOX" "$PA_PBF" -o "$CARLISLE"
else
  echo "    exists, skipping"
fi

echo "==> Done."
ls -lh "$JAR" "$CARLISLE"
