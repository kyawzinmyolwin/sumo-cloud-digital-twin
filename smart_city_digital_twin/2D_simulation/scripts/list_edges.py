#!/usr/bin/env python3
"""
List real (non-internal) edges of a SUMO net so you can pick ids for incidents.

Reads the .net.xml directly (ElementTree — no SUMO/sumolib needed) and prints each
edge's id, street name, length and end nodes. Filter by street name to find the id
to close, e.g. for a --close-edge / --incident-file target:

    # every Hereford Street edge (both directions)
    python scripts/list_edges.py --name "Hereford"

    # edges of a specific scenario's net, longest first, top 20
    python scripts/list_edges.py --sumocfg scenarios/am_2020.sumocfg --sort length --limit 20

Internal edges (ids starting with ':') and edges with a function (connectors) are
skipped — they aren't roads you'd close. A road usually has two directional edges
(e.g. 1015728520#1 and -1015728520#1); pick the direction carrying the traffic you
want to disrupt.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from _sim_root import NETWORK_DIR  # noqa: E402

DEFAULT_NET = NETWORK_DIR / "Christchurch_Central_City_main_streets.net.xml"


def _net_from_sumocfg(sumocfg: Path) -> Path:
    """Resolve the <net-file> referenced by a sumocfg (relative to it)."""
    node = ET.parse(sumocfg).getroot().find(".//net-file")
    value = node.get("value") if node is not None else None
    if not value:
        raise ValueError(f"no <net-file> in {sumocfg}")
    return (sumocfg.parent / value).resolve()


def iter_edges(net_path: Path):
    """Yield (id, name, length, from_node, to_node, n_lanes) for real road edges."""
    for edge in ET.parse(net_path).getroot().iter("edge"):
        eid = edge.get("id", "")
        if eid.startswith(":") or edge.get("function"):
            continue                                  # internal / connector edge
        lanes = edge.findall("lane")
        length = float(lanes[0].get("length")) if lanes and lanes[0].get("length") else 0.0
        yield (eid, edge.get("name", ""), length,
               edge.get("from", ""), edge.get("to", ""), len(lanes))


def main() -> int:
    ap = argparse.ArgumentParser(description="List SUMO net edges (ids for incidents).")
    ap.add_argument("--net", type=Path, default=None, help=f"Net file (default: {DEFAULT_NET.name}).")
    ap.add_argument("--sumocfg", type=Path, default=None,
                    help="Read the net from this scenario config instead of --net.")
    ap.add_argument("--name", default=None, help="Only edges whose street name contains this (case-insensitive).")
    ap.add_argument("--sort", choices=("id", "name", "length"), default="name", help="Sort key (default: name).")
    ap.add_argument("--limit", type=int, default=0, help="Max rows to print (0 = all).")
    args = ap.parse_args()

    net_path = _net_from_sumocfg(args.sumocfg) if args.sumocfg else (args.net or DEFAULT_NET)
    if not net_path.is_file():
        print(f"Error: net file not found: {net_path}", file=sys.stderr)
        return 1

    rows = list(iter_edges(net_path))
    if args.name:
        needle = args.name.lower()
        rows = [r for r in rows if needle in (r[1] or "").lower()]
    key = {"id": lambda r: r[0], "name": lambda r: (r[1], r[0]), "length": lambda r: -r[2]}[args.sort]
    rows.sort(key=key)
    if args.limit > 0:
        rows = rows[: args.limit]

    if not rows:
        print(f"No matching edges in {net_path.name}"
              + (f" for name~'{args.name}'" if args.name else ""), file=sys.stderr)
        return 1

    print(f"{'edge id':<22} {'len(m)':>8}  {'lanes':>5}  street")
    print("-" * 60)
    for eid, name, length, _f, _t, nlanes in rows:
        print(f"{eid:<22} {length:>8.1f}  {nlanes:>5}  {name}")
    print(f"\n{len(rows)} edge(s). Use an id with: "
          f"run_traci.py --close-edge <id>@<start>:<end>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
