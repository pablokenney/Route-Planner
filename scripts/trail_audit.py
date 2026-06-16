#!/usr/bin/env python3
"""Step 1 — LeTort Spring Run + Carlisle borough trail/path OSM coverage audit.

Determines whether the long-run (8-11 mi) use case is mappable BEFORE any routing
engine work. Answers three questions for the area around home:
  1. Are the trails/paths tagged highway=footway/path/cycleway/etc.?
  2. Are they CONNECTED to the street graph (not orphaned islands)?
  3. Are they continuous, or fragmented into disjoint pieces?

Writes a human summary to TRAIL_AUDIT.md and a machine record to
scripts/_trail_audit.json. Run inside the project venv (needs osmnx).
"""
from __future__ import annotations

import json
import os
from collections import Counter

import networkx as nx
import osmnx as ox

HOME = (40.195016, -77.199929)  # lat, lon — confirmed start point
DIST_M = 6000  # ~3.7 mi radius; covers the bbox used for routing
PATH_CLASSES = {"footway", "path", "cycleway", "pedestrian", "steps", "track", "bridleway"}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _highway_of(data) -> list[str]:
    hw = data.get("highway")
    if hw is None:
        return []
    return [str(x) for x in _as_list(hw)]


def _name_of(data) -> str:
    n = data.get("name")
    if n is None:
        return ""
    return " / ".join(str(x) for x in _as_list(n))


def main() -> None:
    print(f"==> Downloading walk network around {HOME}, dist={DIST_M} m ...")
    G = ox.graph_from_point(HOME, dist=DIST_M, network_type="walk", simplify=True)
    G_u = G.to_undirected()
    n_nodes, n_edges = G.number_of_nodes(), G.number_of_edges()
    print(f"    nodes={n_nodes} edges={n_edges}")

    # --- Whole-network connectivity ---
    components = sorted(nx.connected_components(G_u), key=len, reverse=True)
    largest = components[0] if components else set()
    largest_frac = len(largest) / n_nodes if n_nodes else 0.0

    # --- Classify edges; collect path edges and LeTort edges ---
    hw_counter: Counter = Counter()
    path_edges = []      # (u, v) of trail/path-class edges
    letort_edges = []    # edges whose name mentions LeTort
    for u, v, data in G_u.edges(data=True):
        hws = _highway_of(data)
        for h in hws:
            hw_counter[h] += 1
        if any(h in PATH_CLASSES for h in hws):
            path_edges.append((u, v))
        nm = _name_of(data).lower()
        if "letort" in nm or "le tort" in nm:
            letort_edges.append((u, v, _name_of(data), hws))

    # --- Are path edges connected to the MAIN component (the street graph)? ---
    path_nodes = {n for e in path_edges for n in e[:2]}
    path_nodes_in_main = sum(1 for n in path_nodes if n in largest)
    path_nodes_orphaned = len(path_nodes) - path_nodes_in_main

    # --- Fragmentation of the path-only subnetwork ---
    path_subgraph = G_u.edge_subgraph(
        [(u, v, k) for u, v, k in G_u.edges(keys=True) if (u, v) in set(path_edges)]
    ) if path_edges else nx.MultiGraph()
    path_components = (
        sorted((len(c) for c in nx.connected_components(path_subgraph)), reverse=True)
        if path_subgraph.number_of_nodes() else []
    )

    # --- LeTort specifics ---
    letort_nodes = {n for (u, v, *_ ) in letort_edges for n in (u, v)}
    letort_in_main = sum(1 for n in letort_nodes if n in largest)
    letort_names = sorted({nm for (_, _, nm, _) in letort_edges})
    letort_hw = Counter(h for (_, _, _, hws) in letort_edges for h in hws)

    stats = {
        "home": HOME,
        "dist_m": DIST_M,
        "nodes": n_nodes,
        "edges": n_edges,
        "connected_components": len(components),
        "largest_component_fraction": round(largest_frac, 4),
        "highway_tag_counts": dict(hw_counter.most_common()),
        "path_edge_count": len(path_edges),
        "path_nodes": len(path_nodes),
        "path_nodes_in_main_component": path_nodes_in_main,
        "path_nodes_orphaned": path_nodes_orphaned,
        "path_subnetwork_component_sizes_top10": path_components[:10],
        "letort_edge_count": len(letort_edges),
        "letort_names": letort_names,
        "letort_highway_tags": dict(letort_hw),
        "letort_nodes_in_main_component": letort_in_main,
        "letort_nodes_total": len(letort_nodes),
    }

    with open(os.path.join(ROOT, "scripts", "_trail_audit.json"), "w") as f:
        json.dump(stats, f, indent=2)

    _write_markdown(stats)
    print("\n==> Summary")
    print(json.dumps(stats, indent=2))


def _verdict(stats: dict) -> str:
    lines = []
    if stats["letort_edge_count"] == 0:
        lines.append("- ❌ **LeTort not found by name** in OSM within the search radius — "
                     "either untagged, named differently, or outside range. Long-run "
                     "reliance on it is UNCONFIRMED; widen radius or inspect manually.")
    else:
        frac = stats["letort_nodes_in_main_component"] / max(stats["letort_nodes_total"], 1)
        if frac > 0.9:
            lines.append(f"- ✅ **LeTort is well-connected** ({frac:.0%} of its nodes are in "
                         "the main street-graph component) — usable for routed long runs.")
        else:
            lines.append(f"- ⚠️ **LeTort is partly orphaned** (only {frac:.0%} of its nodes "
                         "connect to the main component) — may be fragmented/islanded.")
    if stats["path_nodes_orphaned"] > 0.1 * max(stats["path_nodes"], 1):
        lines.append(f"- ⚠️ **{stats['path_nodes_orphaned']} path nodes are orphaned** from the "
                     "main component — some trails are disconnected islands.")
    else:
        lines.append("- ✅ **Path network is largely connected** to the street graph.")
    if stats["largest_component_fraction"] < 0.95:
        lines.append(f"- ⚠️ Walk graph is fragmented: largest component holds only "
                     f"{stats['largest_component_fraction']:.0%} of nodes.")
    return "\n".join(lines)


def _write_markdown(stats: dict) -> None:
    top_paths = ", ".join(
        f"`{k}`={v}" for k, v in stats["highway_tag_counts"].items() if k in PATH_CLASSES
    ) or "(none)"
    md = f"""# Trail Audit — LeTort Spring Run + Carlisle Borough Paths

Auto-generated by `scripts/trail_audit.py`. Source: OpenStreetMap (OSMnx walk network)
around home {stats['home']}, radius {stats['dist_m']} m.

## Verdict
{_verdict(stats)}

## Network
- Nodes: **{stats['nodes']}**, edges: **{stats['edges']}**
- Connected components: **{stats['connected_components']}** (largest holds
  **{stats['largest_component_fraction']:.1%}** of nodes)

## Path / trail coverage
- Path-class edges (footway/path/cycleway/pedestrian/steps/track/bridleway): **{stats['path_edge_count']}**
- Path-class tag counts: {top_paths}
- Path nodes in main component: **{stats['path_nodes_in_main_component']}** /
  {stats['path_nodes']} (orphaned: **{stats['path_nodes_orphaned']}**)
- Path-only subnetwork component sizes (top 10): {stats['path_subnetwork_component_sizes_top10']}

## LeTort Spring Run specifically
- Edges matched by name: **{stats['letort_edge_count']}**
- Names seen: {stats['letort_names'] or '(none)'}
- highway tags on LeTort edges: {stats['letort_highway_tags'] or '(none)'}
- LeTort nodes connected to main component: **{stats['letort_nodes_in_main_component']}** /
  {stats['letort_nodes_total']}

## Why this matters
Per PLAN.md §5a, 8–11 mi loops on residential-only ≤25 mph streets likely exhaust the
network in a town this size. The LeTort + borough paths are the long-run answer **iff**
they're well-tagged and connected. If the verdict above shows orphaning/fragmentation,
the Phase 2 resolution is a "lollipop" out-and-back on the trail rather than admitting
fast tertiary/unclassified roads.
"""
    with open(os.path.join(ROOT, "TRAIL_AUDIT.md"), "w") as f:
        f.write(md)


if __name__ == "__main__":
    main()
