#!/usr/bin/env python3
"""Resolve Strava segment geometry from the segment IDs in Segments.csv (PLAN.md §Feature 3a).

The CSV (exported from Strava) has IDs + names + distance but NO coordinates. This script
pulls each segment's geometry from Strava's public stream endpoint
(https://www.strava.com/stream/segments/{id}), which returns clean JSON — a `latlng` array
of [lat, lon] points (already decoded), plus `altitude` and `distance` — without auth or
polyline decoding. It writes a runtime cache to data/segments.json; the backend reads ONLY
that cache (this fetch never runs in the request path). Run once (or to refresh) after the
CSV changes.

  python scripts/fetch_segments.py            # all IDs in Segments.csv
  python scripts/fetch_segments.py 3445397    # a single ID (debug)

Caveat: this endpoint is undocumented and used against Strava's ToS for bulk access; it can
change without notice. This is a personal-use convenience. The robust, sanctioned
alternative is the Strava API v3 GET /segments/{id} (read scope, returns map.polyline) —
swap _fetch_latlng for an authenticated API call if this breaks. Any ID that fails to
resolve is logged to a failures file, never silently dropped.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "Segments.csv")
OUT_PATH = os.path.join(ROOT, "data", "segments.json")

# A real desktop UA + XHR header — the stream endpoint expects an in-browser-style request.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}
DELAY_S = 1.5  # polite pause between requests
TIMEOUT_S = 20


def _bbox(latlngs: list) -> list:
    lons = [p[1] for p in latlngs]
    lats = [p[0] for p in latlngs]
    return [min(lons), min(lats), max(lons), max(lats)]  # minlon, minlat, maxlon, maxlat


def _fetch_latlng(seg_id: str) -> list | None:
    """Return [[lat, lon], ...] for a segment id from the stream endpoint, or None on failure."""
    url = f"https://www.strava.com/stream/segments/{seg_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        print(f"  ! {seg_id}: request error {e}")
        return None
    if r.status_code != 200:
        print(f"  ! {seg_id}: HTTP {r.status_code}")
        return None
    try:
        latlng = r.json().get("latlng")
    except ValueError:
        print(f"  ! {seg_id}: non-JSON response (markup change or rate-limited?)")
        return None
    if not latlng or len(latlng) < 2:
        print(f"  ! {seg_id}: no latlng in stream")
        return None
    return [[float(p[0]), float(p[1])] for p in latlng]


def _read_csv(only_id: str | None) -> list[dict]:
    rows = []
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            sid = (row.get("ID") or "").strip()
            if not sid:
                continue
            if only_id and sid != only_id:
                continue
            rows.append({
                "id": int(sid) if sid.isdigit() else sid,
                "name": (row.get("Name") or "").strip(),
                "distance_m": float(row.get("Distance (metres)") or 0.0),
                "grade": float(row["Grade (%)"]) if (row.get("Grade (%)") or "").strip() else None,
            })
    return rows


def main() -> None:
    only_id = sys.argv[1] if len(sys.argv) > 1 else None
    rows = _read_csv(only_id)
    print(f"Resolving {len(rows)} segment(s) -> {OUT_PATH}")
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    resolved, failures = [], []
    for i, row in enumerate(rows, 1):
        sid = row["id"]
        print(f"[{i}/{len(rows)}] {sid}  {row['name']}")
        latlngs = _fetch_latlng(str(sid))
        if not latlngs:
            failures.append({"id": sid, "name": row["name"]})
        else:
            resolved.append({
                "id": sid, "name": row["name"], "distance_m": row["distance_m"],
                "grade": row["grade"], "latlngs": latlngs,
                "start": latlngs[0], "end": latlngs[-1], "bbox": _bbox(latlngs),
            })
        if i < len(rows):
            time.sleep(DELAY_S)

    with open(OUT_PATH, "w") as f:
        json.dump(resolved, f)
    print(f"\nWrote {len(resolved)} segment(s) with geometry to {OUT_PATH}")
    if failures:
        fpath = os.path.join(os.path.dirname(OUT_PATH), "segments_failures.json")
        with open(fpath, "w") as f:
            json.dump(failures, f, indent=2)
        print(f"{len(failures)} unresolved (logged to {fpath}): "
              f"{', '.join(str(x['id']) for x in failures[:10])}"
              f"{'...' if len(failures) > 10 else ''}")


if __name__ == "__main__":
    main()
