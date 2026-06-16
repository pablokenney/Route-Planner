#!/usr/bin/env bash
# Launch GraphHopper (plain Java JAR — no Docker) for the Carlisle bbox.
# First run imports the graph (slow); later runs reuse graphhopper/graph-cache.
#
#   scripts/run_graphhopper.sh          # import (if needed) + serve on :8989
#   scripts/run_graphhopper.sh import   # import only, then exit
#   scripts/run_graphhopper.sh reimport # delete graph-cache and re-import
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GH="$ROOT/graphhopper"
GH_VERSION="10.2"
JAR="$GH/graphhopper-web-${GH_VERSION}.jar"
CONFIG="$GH/config.yml"
CACHE="$GH/graph-cache"

# brew openjdk is keg-only; prefer it, else fall back to PATH java.
if [[ -x /opt/homebrew/opt/openjdk/bin/java ]]; then
  JAVA=/opt/homebrew/opt/openjdk/bin/java
elif command -v java >/dev/null 2>&1; then
  JAVA=java
else
  echo "ERROR: no Java found. Run: brew install openjdk" >&2
  exit 1
fi

[[ -f "$JAR" ]] || { echo "ERROR: $JAR missing. Run scripts/fetch_data.sh first." >&2; exit 1; }
[[ -f "$ROOT/data/carlisle.osm.pbf" ]] || { echo "ERROR: data/carlisle.osm.pbf missing. Run scripts/fetch_data.sh." >&2; exit 1; }

CMD="${1:-server}"
case "$CMD" in
  reimport) rm -rf "$CACHE"; CMD="import" ;;
  import)   ;;
  server)   ;;
  *) echo "usage: $0 [server|import|reimport]" >&2; exit 1 ;;
esac

echo "==> java: $($JAVA -version 2>&1 | head -1)"
echo "==> $CMD using $CONFIG"
cd "$ROOT"
exec "$JAVA" -Xmx4g -jar "$JAR" "$CMD" "$CONFIG"
