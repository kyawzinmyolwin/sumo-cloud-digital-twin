#!/usr/bin/env python3
"""
Build SUMO demand (routes + vehicle flows) from a long-format traffic CSV produced by
``traffic_counts_parser.py``.

Each row with a non-empty ``destination_id`` is a turning movement toward another
intersection id from ``intersection_id``. Counts are summed per

    (origin intersection, destination intersection, wall-clock slot)

using ``date_time`` if present, otherwise ``survey_date`` + ``time`` (+ ``period`` when needed).

using the chosen numeric column (default ``totals``). Trips are routed on the
directed graph implied by ``sumo_plain_edges.edg.xml`` (shortest path by hop count;
typically one edge when neighbours match the counts).

Outputs ``christchurch_demand.rou.xml`` by default (routes + ``<flow>`` elements).

Example::

    python3 sumo_demand_from_traffic_csv.py --traffic-csv traffic_11MAY2026_122048.csv

Simulation (match ``--end`` to the printed horizon, or trim the CSV)::

    sumo -n data/output/network/christchurch_intersections.net.xml -r data/output/demand/christchurch_demand.rou.xml --end 850000
"""

from __future__ import annotations

import argparse
import csv
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from _sim_root import DEMAND_DIR, NETWORK_DIR


def _strip_intersection_id(s: str) -> str:
    return (s or "").strip().strip('"').upper()


def _parse_int(row: dict[str, str], column: str) -> int:
    raw = (row.get(column) or "").strip()
    if raw == "":
        return 0
    return int(float(raw))


def _slot_datetime_from_traffic_time_cell(cell: str) -> datetime | None:
    """Parse ``YYYY-MM-DD HH:MM`` from parser CSV; also accepts legacy ``... (NZST)`` suffix."""
    raw = (cell or "").strip().strip('"')
    if not raw:
        return None
    base = raw.split("(")[0].strip()
    if not base:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M %p"):
        try:
            return datetime.strptime(base, fmt)
        except ValueError:
            continue
    return None


def _slot_datetime_from_traffic_row(row: dict[str, str]) -> datetime | None:
    """Wall-clock slot from ``date_time`` / legacy columns, or ``survey_date`` + ``time`` (+ ``period``)."""
    for key in ("date_time", "survey_date_time"):
        dt = _slot_datetime_from_traffic_time_cell((row.get(key) or "").strip())
        if dt is not None:
            return dt
    survey = (row.get("survey_date") or "").strip().strip('"')
    t = (row.get("time") or "").strip().strip('"')
    if not survey or not t:
        return None
    period = (row.get("period") or "").strip().upper()
    if period in ("AM", "PM") and not any(c in t.lower() for c in ("am", "pm")):
        try:
            return datetime.strptime(f"{survey} {t} {period}", "%Y-%m-%d %I:%M %p")
        except ValueError:
            pass
    return _slot_datetime_from_traffic_time_cell(f"{survey} {t}")


def _am_pm_from_dt(dt: datetime | None) -> str:
    """Rough peak bucket: before noon -> AM, noon and after -> PM."""
    if dt is None:
        return ""
    return "AM" if dt.hour < 12 else "PM"


def load_directed_graph(edges_xml: Path) -> tuple[dict[str, list[tuple[str, str]]], set[str]]:
    """Return adjacency list ``node -> [(successor, edge_id), ...]`` and node id set."""
    tree = ET.parse(edges_xml)
    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    nodes: set[str] = set()
    for el in tree.iter("edge"):
        eid = el.get("id")
        frm = el.get("from")
        to = el.get("to")
        if not eid or not frm or not to:
            continue
        adj[frm].append((to, eid))
        nodes.add(frm)
        nodes.add(to)
    return adj, nodes


def shortest_path_edges(
    adj: dict[str, list[tuple[str, str]]],
    src: str,
    dst: str,
) -> list[str] | None:
    """Minimum-hop directed path; ``edge`` ids only."""
    if src == dst:
        return None
    q: deque[tuple[str, list[str]]] = deque([(src, [])])
    visited: set[str] = {src}
    while q:
        u, edges = q.popleft()
        for v, eid in adj[u]:
            if v in visited:
                continue
            visited.add(v)
            new_edges = edges + [eid]
            if v == dst:
                return new_edges
            q.append((v, new_edges))
    return None


def write_routes_xml(
    path: Path,
    *,
    vtype_id: str,
    routes: list[tuple[str, list[str]]],
    flows: list[tuple[str, str, float, float, int]],
) -> None:
    """``routes``: ``(route_id, [edge_id, ...])``; ``flows``: ``(flow_id, route_id, begin, end, number)``."""
    root = ET.Element("routes")
    ET.SubElement(root, "vType", id=vtype_id, vClass="passenger")
    seen_r: set[str] = set()
    for rid, edges in routes:
        if rid in seen_r:
            continue
        seen_r.add(rid)
        ET.SubElement(root, "route", id=rid, edges=" ".join(edges))

    for fid, rid, begin, end, number in flows:
        ET.SubElement(
            root,
            "flow",
            id=fid,
            type=vtype_id,
            route=rid,
            begin=f"{begin:.2f}",
            end=f"{end:.2f}",
            number=str(number),
        )

    ET.indent(root, space="  ")
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'
    path.write_text(header + ET.tostring(root, encoding="unicode"), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Create SUMO route + flow demand XML from traffic_counts_parser CSV output."
    )
    ap.add_argument(
        "--traffic-csv",
        type=Path,
        required=True,
        help="Long-format CSV (e.g. traffic_DDMMMYYYY_hhmmss.csv).",
    )
    ap.add_argument(
        "--edges-xml",
        type=Path,
        default=NETWORK_DIR / "sumo_plain_edges.edg.xml",
        help="Plain SUMO edges file (default: data/output/network/sumo_plain_edges.edg.xml).",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=DEMAND_DIR / "christchurch_demand.rou.xml",
        help="Output routes file path (default: data/output/demand/christchurch_demand.rou.xml).",
    )
    ap.add_argument(
        "--count-column",
        default="totals",
        help="Numeric column to sum (default: totals).",
    )
    ap.add_argument(
        "--slot-seconds",
        type=float,
        default=900.0,
        help="Length of each count slot in simulation seconds (default: 900 = 15 min).",
    )
    ap.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="Drop aggregated groups with sum strictly below this (default: 1).",
    )
    ap.add_argument(
        "--period",
        choices=("AM", "PM", "ALL"),
        default="ALL",
        help=(
            "Keep only rows in this wall-clock bucket inferred from the parsed slot time "
            "(AM: hour < 12, PM: hour >= 12; default: ALL)."
        ),
    )
    ap.add_argument("--vtype-id", default="passenger", help="vType id written into XML (default: passenger).")
    ap.add_argument(
        "--timeline",
        choices=("stacked", "calendar"),
        default="stacked",
        help=(
            "stacked: each distinct count slot maps to consecutive simulation "
            "windows from t=0 (suited to merged multi-year CSVs). "
            "calendar: use real datetimes (long horizon if many survey days appear)."
        ),
    )
    args = ap.parse_args()

    if not args.traffic_csv.is_file():
        print(f"Error: traffic CSV not found: {args.traffic_csv}", file=sys.stderr)
        return 1
    if not args.edges_xml.is_file():
        print(f"Error: edges XML not found: {args.edges_xml}", file=sys.stderr)
        return 1

    adj, nodes = load_directed_graph(args.edges_xml)

    agg: dict[tuple[str, str, str], int] = defaultdict(int)
    slot_times: dict[tuple[str, str, str], datetime] = {}

    with args.traffic_csv.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if args.count_column not in (reader.fieldnames or []):
            print(
                f"Error: column '{args.count_column}' not in CSV (have: {reader.fieldnames})",
                file=sys.stderr,
            )
            return 1

        for row in reader:
            dst = _strip_intersection_id(row.get("destination_id") or "")
            src = _strip_intersection_id(row.get("intersection_id") or "")
            if not dst or not src:
                continue
            dt_row = _slot_datetime_from_traffic_row(row)
            sdt = dt_row.strftime("%Y-%m-%d %H:%M") if dt_row else ""
            if not sdt:
                sdt = (row.get("date_time") or row.get("survey_date_time") or "").strip()
            if args.period != "ALL":
                dt_bucket = dt_row or _slot_datetime_from_traffic_time_cell(sdt)
                if _am_pm_from_dt(dt_bucket) != args.period:
                    continue
            try:
                n = _parse_int(row, args.count_column)
            except ValueError:
                continue
            if n <= 0:
                continue
            key = (src, dst, sdt)
            agg[key] += n
            if key not in slot_times:
                dt = dt_row or _slot_datetime_from_traffic_time_cell(sdt)
                if dt:
                    slot_times[key] = dt

    if not agg:
        print("Warning: nothing to write (empty aggregation).", file=sys.stderr)
        args.output.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n'
            f'  <vType id="{args.vtype_id}" vClass="passenger"/>\n</routes>\n',
            encoding="utf-8",
        )
        print(f"Wrote empty shell -> {args.output}")
        return 0

    keys_with_time = [k for k in agg if k in slot_times]
    if not keys_with_time:
        print(
            "Error: could not parse any wall-clock slots — expected "
            "date_time or survey_date+time (+period) from traffic_counts_parser.py.",
            file=sys.stderr,
        )
        return 1

    # Time axis is shared by all OD pairs in the same count interval (slot string).
    slot_identity = lambda k: (k[2],)

    slot_begin: dict[tuple[str, ...], float] = {}
    if args.timeline == "calendar":
        identities = sorted(
            {slot_identity(k) for k in keys_with_time},
            key=lambda sid: (
                min(slot_times[k] for k in keys_with_time if slot_identity(k) == sid),
                sid[0],
            ),
        )
        t0_global = min(slot_times[k] for k in keys_with_time)
        for sid in identities:
            rep = next(k for k in keys_with_time if slot_identity(k) == sid)
            dt = slot_times[rep]
            slot_begin[sid] = (dt - t0_global).total_seconds()
        t_last = max(slot_times[k] for k in keys_with_time)
        sim_span = (t_last - t0_global).total_seconds() + args.slot_seconds
    else:
        identities = sorted(
            {slot_identity(k) for k in keys_with_time},
            key=lambda sid: (
                min(slot_times[k] for k in keys_with_time if slot_identity(k) == sid),
                sid[0],
            ),
        )
        for i, sid in enumerate(identities):
            slot_begin[sid] = float(i * args.slot_seconds)
        sim_span = float(len(identities) * args.slot_seconds)

    route_by_od: dict[tuple[str, str], tuple[str, list[str]]] = {}
    od_unreachable: set[tuple[str, str]] = set()
    missing_path = 0
    missing_node = 0
    skipped_small = 0
    skipped_no_time = 0
    flow_rows: list[tuple[str, str, float, float, int]] = []
    route_defs: list[tuple[str, list[str]]] = []
    flow_idx = 0

    for key, count in sorted(agg.items(), key=lambda kv: kv[0]):
        if count < args.min_count:
            skipped_small += 1
            continue
        src, dst, _sdt = key
        if src not in nodes or dst not in nodes:
            missing_node += 1
            continue
        od = (src, dst)
        if od not in route_by_od:
            if od in od_unreachable:
                continue
            path = shortest_path_edges(adj, src, dst)
            if not path:
                od_unreachable.add(od)
                missing_path += 1
                continue
            rid = f"r_{src}_{dst}"
            route_by_od[od] = (rid, path)
            route_defs.append((rid, path))

        rid, _edges = route_by_od[od]
        sid = slot_identity(key)
        if sid not in slot_begin:
            skipped_no_time += 1
            continue
        begin = slot_begin[sid]
        end = begin + args.slot_seconds
        fid = f"f_{flow_idx}"
        flow_idx += 1
        flow_rows.append((fid, rid, begin, end, count))

    # Expand route_defs unique
    seen: set[str] = set()
    route_defs_unique: list[tuple[str, list[str]]] = []
    for rid, eds in route_defs:
        if rid not in seen:
            seen.add(rid)
            route_defs_unique.append((rid, eds))

    flows_for_xml: list[tuple[str, str, float, float, int]] = [
        (fid, rid, b, e, n) for fid, rid, b, e, n in flow_rows
    ]

    write_routes_xml(
        args.output,
        vtype_id=args.vtype_id,
        routes=route_defs_unique,
        flows=flows_for_xml,
    )

    n_veh = sum(f[4] for f in flows_for_xml)
    print(f"Wrote {args.output}")
    print(f"  Timeline: {args.timeline}")
    print(f"  Simulation horizon (approx): 0 .. {sim_span:.0f} s ({sim_span/3600:.2f} h)")
    print(f"  Unique routes: {len(route_defs_unique)}")
    print(f"  Flow elements: {len(flows_for_xml)}")
    print(f"  Total vehicles (number sum): {n_veh}")
    if skipped_small:
        print(f"  Skipped (count < {args.min_count}): {skipped_small}")
    if missing_node:
        print(f"  Skipped (endpoint not in network): {missing_node}")
    if missing_path:
        print(f"  Skipped (no directed path): {missing_path} OD pairs")
    if skipped_no_time:
        print(f"  Skipped (unparsed time slot): {skipped_no_time}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
