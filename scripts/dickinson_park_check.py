#!/usr/bin/env python3
"""Standalone diagnostic — does the router include Dickinson Park's internal trails?

Tests the LOCAL OSM data GraphHopper actually uses (data/carlisle.osm.pbf), not Google
and not live Overpass. Runs three layers in order so a failure localizes itself:

  LAYER 1  Existence    — count highway=path/footway/cycleway ways inside the park (osmium)
  LAYER 2  Connectivity — are those park-path nodes in the same component as the streets,
                          and is there a shared node (access point) with a street? (coord-
                          identity graph over the local extract = GraphHopper's own notion
                          of connectivity via shared OSM nodes)
  LAYER 3  Routing      — point-to-point on the `run` profile between two access points on
                          opposite sides of the park; does the geometry cut THROUGH (path/
                          footway edges inside the park bbox) or skirt around?

Re-runnable: `.venv/bin/python scripts/dickinson_park_check.py` (needs GraphHopper on :8989).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict, deque

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBF = os.path.join(ROOT, "data", "carlisle.osm.pbf")
GH = os.environ.get("GH_URL", "http://localhost:8989")

# "Dickinson Park Intramural Fields" (leisure=park) polygon bbox from the OSM extract,
# anchored by Woodward Drive along its south edge.  (S, W, N, E)
PARK = (40.19800, -77.21723, 40.20183, -77.21007)
PAD = 0.003  # ~250-330 m halo of surrounding streets for the connectivity context
REGION = (PARK[0] - PAD, PARK[1] - PAD, PARK[2] + PAD, PARK[3] + PAD)

PATH_CLASSES = {"path", "footway", "cycleway"}          # Layer 1 focus (per task)
ROUTE_PATH_CLASSES = {"footway", "path", "cycleway", "pedestrian", "track", "steps"}


# ----------------------------------------------------------------------------- helpers
def in_bbox(lat, lon, bb) -> bool:
    return bb[0] <= lat <= bb[2] and bb[1] <= lon <= bb[3]


def _osmium_geojson(bb) -> list[dict]:
    """Clip the extract to bbox bb=(S,W,N,E) and return its GeoJSON features."""
    with tempfile.TemporaryDirectory() as tmp:
        clip = os.path.join(tmp, "clip.osm.pbf")
        gj = os.path.join(tmp, "clip.geojsonl")
        subprocess.run(["osmium", "extract", "-b", f"{bb[1]},{bb[0]},{bb[3]},{bb[2]}",
                        PBF, "-o", clip, "--overwrite"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["osmium", "export", clip, "-f", "geojsonseq", "-o", gj,
                        "--overwrite"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        feats = []
        for line in open(gj):
            line = line.strip().lstrip("\x1e")
            if line:
                feats.append(json.loads(line))
        return feats


def _highway_of(p) -> str | None:
    hw = p.get("highway")
    if isinstance(hw, list):
        hw = hw[0] if hw else None
    return str(hw).lower() if hw else None


# ------------------------------------------------------------------------------ Layer 1
def layer1_existence() -> list[dict]:
    feats = _osmium_geojson(PARK)
    found = []
    for f in feats:
        g = f.get("geometry", {})
        if g.get("type") != "LineString":
            continue
        p = f.get("properties", {})
        hw = _highway_of(p)
        if hw not in PATH_CLASSES:
            continue
        # keep ways with at least one vertex inside the park bbox
        cs = g["coordinates"]
        if not any(in_bbox(c[1], c[0], PARK) for c in cs):
            continue
        found.append({
            "name": p.get("name", ""), "highway": hw,
            "foot": p.get("foot"), "access": p.get("access"),
            "surface": p.get("surface"), "bicycle": p.get("bicycle"),
            "n_pts": len(cs),
        })
    print("=" * 78)
    print("LAYER 1 — EXISTENCE (highway=path/footway/cycleway inside park bbox)")
    print("=" * 78)
    print(f"park bbox (S,W,N,E) = {PARK}")
    print(f"path/footway/cycleway ways with a vertex in the park: {len(found)}")
    blockers = 0
    for w in found:
        flags = {k: w[k] for k in ("foot", "access", "bicycle", "surface") if w[k] is not None}
        if w.get("foot") in ("no", "private") or w.get("access") in ("no", "private"):
            blockers += 1
            flags["BLOCKS_FOOT"] = True
        print(f"  - {w['highway']:8} {('«' + w['name'] + '»') if w['name'] else '(unnamed)':28} "
              f"pts={w['n_pts']:3}  {flags or ''}")
    if blockers:
        print(f"  ⚠ {blockers} way(s) carry an access/foot tag that could block routing")
    return found


# ------------------------------------------------------------------------------ Layer 2
def _coord_key(c) -> tuple[int, int]:
    # exact OSM node sharing == identical coords; round to ~1e-7 deg (~1cm)
    return (round(c[0], 7), round(c[1], 7))


def layer2_connectivity() -> dict:
    feats = _osmium_geojson(REGION)
    adj: dict = defaultdict(set)         # coord-key -> neighbour coord-keys
    node_is_park_path: dict = {}         # coord-key -> bool (on a park path way)
    node_is_street: dict = defaultdict(bool)
    park_path_nodes: set = set()
    access_nodes: list = []              # park-path nodes shared with a street way

    way_nodes = []  # (is_park_path, is_street, [coord_keys], latlon for park nodes)
    for f in feats:
        g = f.get("geometry", {})
        if g.get("type") != "LineString":
            continue
        hw = _highway_of(f.get("properties", {}))
        if hw is None:
            continue
        cs = g["coordinates"]
        keys = [_coord_key(c) for c in cs]
        for a, b in zip(keys, keys[1:]):
            adj[a].add(b)
            adj[b].add(a)
        is_park_path = (hw in ROUTE_PATH_CLASSES) and any(in_bbox(c[1], c[0], PARK) for c in cs)
        is_street = hw not in ROUTE_PATH_CLASSES  # roads/streets (not foot-only paths)
        for c, k in zip(cs, keys):
            if is_park_path and in_bbox(c[1], c[0], PARK):
                node_is_park_path[k] = (c[1], c[0])
                park_path_nodes.add(k)
            if is_street:
                node_is_street[k] = True

    # connected components over the whole region graph
    seen: set = set()
    comp_id: dict = {}
    cid = 0
    for start in adj:
        if start in seen:
            continue
        cid += 1
        q = deque([start])
        seen.add(start)
        while q:
            u = q.popleft()
            comp_id[u] = cid
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    q.append(v)
    # which component holds the most street nodes = "the street network"
    street_comp_sizes: dict = defaultdict(int)
    for k, v in node_is_street.items():
        if v:
            street_comp_sizes[comp_id.get(k)] += 1
    main_comp = max(street_comp_sizes, key=street_comp_sizes.get) if street_comp_sizes else None

    park_in_main = sum(1 for k in park_path_nodes if comp_id.get(k) == main_comp)
    # access nodes: a park-path coord that is ALSO a street node (shared OSM node)
    for k in park_path_nodes:
        if node_is_street.get(k):
            lat, lon = node_is_park_path[k]
            access_nodes.append((lat, lon))

    print("\n" + "=" * 78)
    print("LAYER 2 — CONNECTIVITY (coord-identity graph over the local extract)")
    print("=" * 78)
    print(f"region bbox (S,W,N,E) = {tuple(round(x,5) for x in REGION)}")
    print(f"park-path nodes: {len(park_path_nodes)}")
    print(f"  in the main street component: {park_in_main} / {len(park_path_nodes)}")
    print(f"shared access nodes (park path == street node): {len(access_nodes)}")
    for lat, lon in sorted(access_nodes)[:8]:
        print(f"    access @ ({lat:.6f}, {lon:.6f})")
    connected = (len(park_path_nodes) > 0 and park_in_main > 0)
    print(f"VERDICT (layer 2): {'CONNECTED' if connected else 'ORPHANED / not connected'}")
    return {
        "park_path_nodes": len(park_path_nodes),
        "park_in_main": park_in_main,
        "access_nodes": access_nodes,
        "connected": connected,
    }


# ------------------------------------------------------------------------------ Layer 3
def _gh_route(a, b):
    r = requests.get(f"{GH}/route", params={
        "point": [f"{a[0]},{a[1]}", f"{b[0]},{b[1]}"], "profile": "run",
        "ch.disable": "true", "details": "road_class",
        "points_encoded": "false", "instructions": "false",
    }, timeout=30)
    return r


def _pick_cross_park_endpoints(access_nodes) -> tuple[tuple, tuple, str]:
    """Two points on opposite (S/N) sides so the direct line crosses the park interior."""
    if len(access_nodes) >= 2:
        south = min(access_nodes, key=lambda x: x[0])
        north = max(access_nodes, key=lambda x: x[0])
        if north[0] - south[0] > 0.0008:  # ~90 m apart N-S
            return south, north, "park access nodes (S vs N)"
    # fallback: bbox edge midpoints just outside the park S and N
    s = (PARK[0] - 0.0006, (PARK[1] + PARK[3]) / 2)
    n = (PARK[2] + 0.0006, (PARK[1] + PARK[3]) / 2)
    return s, n, "park bbox S/N edge midpoints (fallback)"


def layer3_routing(access_nodes) -> dict:
    a, b, how = _pick_cross_park_endpoints(access_nodes)
    print("\n" + "=" * 78)
    print("LAYER 3 — ROUTING (point-to-point on `run`, decisive)")
    print("=" * 78)
    print(f"endpoints via {how}:\n  S = {a}\n  N = {b}")
    r = _gh_route(a, b)
    if r.status_code != 200:
        print(f"  run profile returned {r.status_code}: {r.text[:160]}")
        return {"routed": False, "cuts_through": False, "a": a, "b": b}
    path = r.json()["paths"][0]
    cs = path["points"]["coordinates"]
    total_path_m_in_park = 0.0
    path_classes_in_park = set()
    for fr, to, cls in path.get("details", {}).get("road_class", []):
        c = str(cls).lower()
        if c not in ROUTE_PATH_CLASSES:
            continue
        for i in range(fr, to):
            mid = ((cs[i][1] + cs[i + 1][1]) / 2, (cs[i][0] + cs[i + 1][0]) / 2)
            if in_bbox(mid[0], mid[1], PARK):
                seg = _hav(cs[i], cs[i + 1])
                total_path_m_in_park += seg
                path_classes_in_park.add(c)
    cuts_through = total_path_m_in_park > 20.0  # >20 m of path/footway inside the park
    print(f"  route distance: {path['distance']:.0f} m")
    print(f"  path/footway/cycleway distance INSIDE park bbox: {total_path_m_in_park:.0f} m "
          f"({sorted(path_classes_in_park) or 'none'})")
    print(f"VERDICT (layer 3): {'CUTS THROUGH the park' if cuts_through else 'SKIRTS AROUND'}")
    return {"routed": True, "cuts_through": cuts_through, "a": a, "b": b,
            "path_m_in_park": round(total_path_m_in_park, 1)}


def _hav(p, q) -> float:
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [p[1], p[0], q[1], q[0]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# --------------------------------------------------------------------------------- main
def main() -> None:
    if not os.path.exists(PBF):
        sys.exit(f"missing {PBF}")
    l1 = layer1_existence()
    l2 = layer2_connectivity()
    l3 = layer3_routing(l2["access_nodes"])

    print("\n" + "#" * 78)
    print("VERDICT")
    print("#" * 78)
    if l3["cuts_through"]:
        print("✅ WORKING AS INTENDED — the router cuts through Dickinson Park on its trails.")
    elif not l1:
        print("❌ TRAILS NOT IN OSM — Layer 1 found no park paths; nothing to route onto.")
    elif not l2["connected"]:
        print("❌ ORPHANED — park trails exist (Layer 1) but are NOT connected to the street "
              "graph (Layer 2); the router can't reach them, so it goes around.")
    else:
        print("⚠️  GOES AROUND despite existing+connected trails — likely an access/foot tag "
              "blocks them on the `run` profile, or the cross-park line isn't actually "
              "shortest. Inspect Layer 1 tags and Layer 3 endpoints.")
    # signal for the conditional regression guard
    print(f"\n[layers_pass_for_test={bool(l1) and l2['connected']}] "
          f"[cuts_through={l3['cuts_through']}]")
    if l2["access_nodes"]:
        a, b, _ = _pick_cross_park_endpoints(l2["access_nodes"])
        print(f"[cross_park_endpoints S={a} N={b}]")


if __name__ == "__main__":
    main()
