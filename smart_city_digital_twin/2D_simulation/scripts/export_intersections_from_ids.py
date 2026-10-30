#!/usr/bin/env python3
"""
Option 1 (intersection IDs):

Match junction IDs in SUMO net.xml to node IDs in OSM osm.xml, producing a big list:
  id, lon, lat, sumo_x, sumo_y

This is the cleanest ground truth because SUMO junctions are the network skeleton.
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from _sim_root import PROJECT_ROOT as root


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", type=Path, default=root / "2D_simulation/data/output/network/Christchurch_Central_City_main_streets.net.xml")
    ap.add_argument("--osm", type=Path, default=root / "2D_simulation/data/output/network/Christchurch_Central_City_main_streets.osm.xml")
    ap.add_argument(
        "--out",
        type=Path,
        default=root / "3D_simulation/Assets/StreamingAssets/Christchurch/intersections.json",
    )
    args = ap.parse_args()

    net_root = ET.parse(args.net).getroot()
    osm_root = ET.parse(args.osm).getroot()

    # OSM nodes (id -> (lon,lat))
    osm_nodes: dict[str, tuple[float, float]] = {}
    for n in osm_root.findall("node"):
        nid = n.get("id")
        lon = n.get("lon")
        lat = n.get("lat")
        if nid and lon and lat:
            osm_nodes[nid] = (float(lon), float(lat))

    rows = []
    matched = 0
    for j in net_root.findall("junction"):
        if j.get("type") == "internal":
            continue
        jid = j.get("id")
        x = j.get("x")
        y = j.get("y")
        if not jid or x is None or y is None:
            continue
        if jid not in osm_nodes:
            continue
        lon, lat = osm_nodes[jid]
        rows.append(
            {
                "id": jid,
                "longitude": lon,
                "latitude": lat,
                "sumo_x": float(x),
                "sumo_y": float(y),
            }
        )
        matched += 1

    rows.sort(key=lambda r: r["id"])
    out = {
        "source": {"net": str(args.net), "osm": str(args.osm)},
        "matched_junctions": matched,
        "intersections": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Matched junctions: {matched}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

