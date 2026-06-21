"""Build avoidance polygons for unsafe roads (PLAN.md §safety follow-up).

Reads the local OSM extract, pulls the geometry of each road named in BLACKLIST_NAMES,
buffers each into a thin polygon, and writes data/blacklist_areas.json as a GeoJSON
FeatureCollection in GraphHopper custom-model `areas` form (each feature carries an `id`
so the backend can reference it as `in_<id>` and zero its priority).

This is the RIGHT layer to block a road: GraphHopper then routes *around* these segments
while still freely using every nearby trail (the LeTort / Cress Bed loops survive), instead
of post-filtering whole candidate routes that merely clip a bad road.

Re-run after editing BLACKLIST_NAMES:  python scripts/build_blacklist.py
Requires the `osmium` CLI (already used elsewhere in this project) and data/carlisle.osm.pbf.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBF = os.path.join(ROOT, "data", "carlisle.osm.pbf")
OUT = os.path.join(ROOT, "data", "blacklist_areas.json")

# Exact OSM `name` tags to avoid. Add a road here and re-run to block it.
BLACKLIST_NAMES = {"Heisers Lane", "Bonnybrook Road"}

# Half-width of the avoidance sleeve around each road centerline. Kept tight (covers the
# roadway + shoulder) so it blocks the road itself without clipping nearby trails that run
# parallel — e.g. the LeTort Nature Trail's south end is ~21 m from Heisers Lane, so a wider
# buffer would swallow the trail tip and sever it. Keep BUFFER_M comfortably below that.
BUFFER_M = 14.0
MAX_PTS = 40         # decimate long roads to keep request payloads small


def _named_linestrings() -> list[tuple[str, list]]:
    """[(name, [[lon,lat],...]), ...] for every way whose name is in BLACKLIST_NAMES."""
    with tempfile.TemporaryDirectory() as td:
        named = os.path.join(td, "named.osm.pbf")
        seq = os.path.join(td, "named.geojsonseq")
        subprocess.run(["osmium", "tags-filter", PBF, "w/name", "-o", named, "--overwrite"],
                       check=True, capture_output=True)
        subprocess.run(["osmium", "export", named, "-f", "geojsonseq", "-o", seq, "--overwrite"],
                       check=True, capture_output=True)
        out = []
        with open(seq) as f:
            for line in f:
                line = line.lstrip("\x1e").strip()
                if not line:
                    continue
                feat = json.loads(line)
                name = (feat.get("properties") or {}).get("name")
                geom = feat.get("geometry") or {}
                if name in BLACKLIST_NAMES and geom.get("type") == "LineString":
                    out.append((name, geom["coordinates"]))
        return out


def _decimate(coords: list, max_pts: int) -> list:
    if len(coords) <= max_pts:
        return coords
    step = math.ceil(len(coords) / max_pts)
    kept = coords[::step]
    if kept[-1] != coords[-1]:
        kept.append(coords[-1])
    return kept


def _buffer_polygon(coords: list, buf_m: float) -> list:
    """Thin sleeve polygon around a polyline. Offsets each vertex along the average segment
    normal, walks the left side out and the right side back, with small tangent end-caps."""
    coords = _decimate(coords, MAX_PTS)
    lat0 = sum(c[1] for c in coords) / len(coords)
    mlat, mlon = 111320.0, 111320.0 * math.cos(math.radians(lat0))

    def to_m(c):
        return (c[0] * mlon, c[1] * mlat)

    def to_ll(x, y):
        return [x / mlon, y / mlat]

    pts = [to_m(c) for c in coords]

    # Per-vertex unit normal = perpendicular to the mean of adjacent segment directions.
    normals = []
    for i in range(len(pts)):
        dx = dy = 0.0
        if i > 0:
            ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
            n = math.hypot(ax, ay) or 1.0
            dx += ax / n; dy += ay / n
        if i < len(pts) - 1:
            bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
            n = math.hypot(bx, by) or 1.0
            dx += bx / n; dy += by / n
        n = math.hypot(dx, dy) or 1.0
        tx, ty = dx / n, dy / n           # unit tangent
        normals.append((-ty, tx))         # left normal

    # Extend the two endpoints along their tangent so the sleeve caps the road ends.
    pts = list(pts)
    if len(pts) >= 2:
        t0 = (pts[0][0] - pts[1][0], pts[0][1] - pts[1][1])
        n = math.hypot(*t0) or 1.0
        pts[0] = (pts[0][0] + t0[0] / n * buf_m, pts[0][1] + t0[1] / n * buf_m)
        t1 = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
        n = math.hypot(*t1) or 1.0
        pts[-1] = (pts[-1][0] + t1[0] / n * buf_m, pts[-1][1] + t1[1] / n * buf_m)

    left = [(pts[i][0] + normals[i][0] * buf_m, pts[i][1] + normals[i][1] * buf_m)
            for i in range(len(pts))]
    right = [(pts[i][0] - normals[i][0] * buf_m, pts[i][1] - normals[i][1] * buf_m)
             for i in range(len(pts))]
    ring_m = left + right[::-1] + [left[0]]
    return [to_ll(x, y) for x, y in ring_m]


def main() -> None:
    lines = _named_linestrings()
    if not lines:
        raise SystemExit(f"No ways matched {BLACKLIST_NAMES} in {PBF}")
    features = []
    for i, (name, coords) in enumerate(lines):
        ring = _buffer_polygon(coords, BUFFER_M)
        features.append({
            "type": "Feature",
            "id": f"bl{i}",
            "properties": {"name": name},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    fc = {"type": "FeatureCollection", "features": features}
    with open(OUT, "w") as f:
        json.dump(fc, f)
    names = sorted({n for n, _ in lines})
    print(f"Wrote {len(features)} avoidance polygons for {names} -> {OUT}")


if __name__ == "__main__":
    main()
