#!/usr/bin/env python3
"""
Relate a SUMO edge id back to the surveyed intersection(s) it belongs to.

The build maps each intersection to its nearby net edges
(``data/output/network/intersection_to_edges.csv``); this inverts that into an
edge -> intersection lookup, and adds the edge's own street name (from the
``.net.xml``) and the intersection's street names
(``data/input/intersection_streets.csv``). Useful for labelling a road-block /
scenario edge in a human-readable way (e.g. for the report or the dashboard):

    # what road + intersection is this edge?  (use = so argparse doesn't read the
    # leading '-' as a flag)
    python scripts/edge_to_intersection.py --edge=-139811056#1

    # dump the full edge -> intersection table as CSV
    python scripts/edge_to_intersection.py -o edge_to_intersection.csv

A directional edge (``-139811056#1``) and its reverse (``139811056#1``) are the
same physical road, so matching is sign-insensitive. An edge usually sits between
TWO intersections, so a lookup can list more than one, nearest first (by the
distance the mapping recorded).

Pure helpers (``normalize_edge``, ``parse_candidates``, ``build_reverse``) are
unit-tested in tests/test_edge_to_intersection.py; only the loaders touch files.
"""
from __future__ import annotations

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MAP = HERE.parent / "data/output/network/intersection_to_edges.csv"
DEFAULT_STREETS = HERE.parent / "data/input/intersection_streets.csv"
DEFAULT_NET = HERE.parent / "data/output/network/Christchurch_Central_City_main_streets.net.xml"


def normalize_edge(edge: str) -> str:
    """Direction-insensitive key for an edge id: drop a single leading '-'."""
    e = edge.strip()
    return e[1:] if e.startswith("-") else e


def parse_candidates(cell: str) -> list[tuple[str, float]]:
    """Parse a ``candidates`` cell ('edge:0.47m;edge2:3.24m') -> [(edge, dist_m)]."""
    out: list[tuple[str, float]] = []
    for part in (cell or "").split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        eid, dist = part.rsplit(":", 1)
        dist = dist.strip().rstrip("m")
        try:
            out.append((eid.strip(), float(dist)))
        except ValueError:
            continue
    return out


def _dist(row: dict) -> float:
    try:
        return float(row.get("dist_m", "") or "nan")
    except ValueError:
        return float("nan")


def build_reverse(rows: list[dict]) -> dict[str, list[dict]]:
    """Invert intersection->edges rows into {normalized_edge: [ {intersection,
    role, dist_m}, ... ]}, nearest first, de-duplicated per (edge, intersection)."""
    acc: dict[str, dict[str, dict]] = {}

    def add(edge: str, inter: str, role: str, dist: float) -> None:
        if not edge or not inter:
            return
        key = normalize_edge(edge)
        slot = acc.setdefault(key, {})
        cur = slot.get(inter)
        # keep the closest / most specific role per (edge, intersection)
        if cur is None or (dist == dist and dist < cur["dist_m"]):
            slot[inter] = {"intersection": inter, "role": role, "dist_m": dist}

    for row in rows:
        inter = (row.get("intersection_id") or "").strip()
        base = _dist(row)
        for col, role in (("nearest_edge", "nearest"), ("enter_edge", "enter"), ("exit_edge", "exit")):
            add((row.get(col) or "").strip(), inter, role, base)
        for eid, d in parse_candidates(row.get("candidates", "")):
            add(eid, inter, "candidate", d)

    out: dict[str, list[dict]] = {}
    for edge, slot in acc.items():
        out[edge] = sorted(
            slot.values(),
            key=lambda r: (r["dist_m"] if r["dist_m"] == r["dist_m"] else float("inf")),
        )
    return out


def load_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_streets(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            streets = [r.get(f"street_{i}", "").strip() for i in range(1, 5)]
            out[(r.get("id") or "").strip()] = " / ".join(s for s in streets if s)
    return out


def edge_names(net_path: Path) -> dict[str, str]:
    """{edge_id: street name} from the .net.xml (raw ids, both directions)."""
    out: dict[str, str] = {}
    if not Path(net_path).exists():
        return out
    for _, el in ET.iterparse(str(net_path), events=("end",)):
        if el.tag == "edge" and el.get("function") is None and el.get("name"):
            out[el.get("id")] = el.get("name")
        if el.tag == "edge":
            el.clear()
    return out


def describe(edge: str, reverse: dict, streets: dict, names: dict) -> str:
    key = normalize_edge(edge)
    road = names.get(edge) or names.get(key) or names.get("-" + key) or "(unnamed)"
    lines = [f"edge {edge}  —  street: {road}"]
    hits = reverse.get(key, [])
    if not hits:
        lines.append("  no intersection match in intersection_to_edges.csv")
        return "\n".join(lines)
    for h in hits:
        st = streets.get(h["intersection"], "")
        d = "" if h["dist_m"] != h["dist_m"] else f"{h['dist_m']:.2f} m"
        lines.append(f"  {h['intersection']:7s} [{h['role']:9s} {d:>8s}]  {st}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Edge id -> intersection(s) + street names.")
    p.add_argument("--edge", help="Look up a single edge id (e.g. -139811056#1).")
    p.add_argument("--map", default=str(DEFAULT_MAP), help="intersection_to_edges.csv")
    p.add_argument("--streets", default=str(DEFAULT_STREETS), help="intersection_streets.csv")
    p.add_argument("--net", default=str(DEFAULT_NET), help=".net.xml (for edge street names)")
    p.add_argument("-o", "--out", help="Write the full edge->intersection table to this CSV.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    reverse = build_reverse(load_rows(Path(args.map)))
    streets = load_streets(Path(args.streets))
    names = edge_names(Path(args.net))

    if args.edge:
        print(describe(args.edge, reverse, streets, names))
        return 0

    # full dump (CSV): one row per (edge, intersection)
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    w = csv.writer(out)
    w.writerow(["edge", "street_name", "intersection_id", "intersection_streets", "role", "dist_m"])
    for edge in sorted(reverse):
        road = names.get(edge) or names.get("-" + edge) or ""
        for h in reverse[edge]:
            d = "" if h["dist_m"] != h["dist_m"] else f"{h['dist_m']:.2f}"
            w.writerow([edge, road, h["intersection"], streets.get(h["intersection"], ""), h["role"], d])
    if args.out:
        out.close()
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
