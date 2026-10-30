#!/usr/bin/env python3
"""
Build a SUMO road network from ``intersection_geo.csv``.

Uses NZTM easting/northing (``X``, ``Y``, EPSG:2193) as Cartesian metres — consistent with
Christchurch OpenData. Connectivity follows the directional neighbour columns
(``intersection_id_*``): each pair is linked by two directed edges (both ways).

Writes plain XML to ``data/output/network/``:

- ``sumo_plain_nodes.nod.xml``
- ``sumo_plain_edges.edg.xml``

Then runs ``netconvert`` to produce ``christchurch_intersections.net.xml`` unless ``--no-netconvert``.

Example (from ``2D_simulation/``)::

    python3 scripts/sumo_network_from_geo.py
    python3 scripts/sumo_network_from_geo.py --csv data/output/intersection_geo.csv --output-net data/output/network/my.net.xml
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from _sim_root import DATA_OUTPUT_DIR, NETWORK_DIR

NEIGHBOUR_KEYS = [
    "intersection_id_S",
    "intersection_id_SW",
    "intersection_id_W",
    "intersection_id_NW",
    "intersection_id_N",
    "intersection_id_NE",
    "intersection_id_E",
    "intersection_id_SE",
]


def _strip_id(s: str) -> str:
    return (s or "").strip().upper()


def load_coords_and_pairs(csv_path: Path) -> tuple[dict[str, tuple[float, float]], set[frozenset[str]]]:
    """Return node coordinates (NZTM) and unique undirected neighbour pairs."""
    coords: dict[str, tuple[float, float]] = {}
    pairs: set[frozenset[str]] = set()

    with csv_path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)

    for row in rows:
        iid = _strip_id(row.get("intersection_id", ""))
        if not iid:
            continue
        try:
            x = float(row["X"])
            y = float(row["Y"])
        except (KeyError, ValueError):
            continue
        coords[iid] = (x, y)

    for row in rows:
        iid = _strip_id(row.get("intersection_id", ""))
        if not iid or iid not in coords:
            continue
        for key in NEIGHBOUR_KEYS:
            nid = _strip_id(row.get(key, ""))
            if not nid or nid not in coords:
                continue
            if nid == iid:
                continue
            pairs.add(frozenset((iid, nid)))

    return coords, pairs


def write_plain_nodes(path: Path, coords: dict[str, tuple[float, float]]) -> None:
    root = ET.Element("nodes")
    for iid in sorted(coords.keys()):
        x, y = coords[iid]
        ET.SubElement(
            root,
            "node",
            {"id": iid, "x": f"{x:.3f}", "y": f"{y:.3f}"},
        )
    ET.indent(root, space="  ")
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
        encoding="utf-8",
    )


def write_plain_edges(
    path: Path,
    coords: dict[str, tuple[float, float]],
    pairs: set[frozenset[str]],
    *,
    num_lanes: int,
    speed_m_s: float,
    priority: str,
) -> None:
    root = ET.Element("edges")
    for pair in sorted(pairs, key=lambda p: (min(p), max(p))):
        a, b = tuple(sorted(pair))
        for frm, to in ((a, b), (b, a)):
            eid = f"e_{frm}_{to}"
            ET.SubElement(
                root,
                "edge",
                {
                    "id": eid,
                    "from": frm,
                    "to": to,
                    "numLanes": str(num_lanes),
                    "speed": f"{speed_m_s:.2f}",
                    "priority": priority,
                },
            )
    ET.indent(root, space="  ")
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
        encoding="utf-8",
    )


def run_netconvert(
    nodes: Path,
    edges: Path,
    out_net: Path,
    *,
    netconvert: str,
) -> int:
    cmd = [
        netconvert,
        "-n",
        str(nodes),
        "-e",
        str(edges),
        "-o",
        str(out_net),
        "--no-turnarounds",
    ]
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, check=False)
    return r.returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build SUMO plain nodes/edges (and optional .net.xml) from intersection_geo.csv."
    )
    ap.add_argument(
        "--csv",
        type=Path,
        default=DATA_OUTPUT_DIR / "intersection_geo.csv",
        help="Path to intersection_geo.csv (default: data/output/intersection_geo.csv).",
    )
    ap.add_argument(
        "--output-net",
        type=Path,
        default=NETWORK_DIR / "christchurch_intersections.net.xml",
        help="Output SUMO network path.",
    )
    ap.add_argument(
        "--no-netconvert",
        action="store_true",
        help="Only write plain nodes/edges; do not run netconvert.",
    )
    ap.add_argument(
        "--netconvert",
        default="netconvert",
        help="netconvert executable (default: netconvert on PATH).",
    )
    ap.add_argument("--num-lanes", type=int, default=2, help="Lanes per direction (default: 2).")
    ap.add_argument(
        "--speed-kmh",
        type=float,
        default=50.0,
        help="Edge speed in km/h (default: 50).",
    )
    ap.add_argument("--priority", default="1", help="Edge priority string (default: 1).")
    args = ap.parse_args()

    if not args.csv.is_file():
        print(f"Error: CSV not found: {args.csv}", file=sys.stderr)
        return 1

    coords, pairs = load_coords_and_pairs(args.csv)
    if not coords:
        print("Error: no nodes with valid X/Y in CSV.", file=sys.stderr)
        return 1

    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    nodes_path = NETWORK_DIR / "sumo_plain_nodes.nod.xml"
    edges_path = NETWORK_DIR / "sumo_plain_edges.edg.xml"

    speed_m_s = max(0.1, args.speed_kmh) / 3.6
    write_plain_nodes(nodes_path, coords)
    write_plain_edges(
        edges_path,
        coords,
        pairs,
        num_lanes=args.num_lanes,
        speed_m_s=speed_m_s,
        priority=args.priority,
    )

    n_edges = len(pairs) * 2
    print(f"Wrote {len(coords)} nodes -> {nodes_path}")
    print(f"Wrote {n_edges} directed edges ({len(pairs)} undirected links) -> {edges_path}")

    if args.no_netconvert:
        return 0

    args.output_net.parent.mkdir(parents=True, exist_ok=True)
    code = run_netconvert(nodes_path, edges_path, args.output_net, netconvert=args.netconvert)
    if code == 0:
        print(f"Wrote {args.output_net}")
    else:
        print(f"netconvert exited with {code}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
