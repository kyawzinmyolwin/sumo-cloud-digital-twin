"""Export SUMO edges for OSM Metro bus routes and simulation bus OD routes."""
from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from _sim_root import DATA_OUTPUT_DIR, DEMAND_DIR, NETWORK_DIR

OSM = NETWORK_DIR / "Christchurch_Central_City.osm.xml"
NET = NETWORK_DIR / "Christchurch_Central_City_main_streets.net.xml"
ROUTED = DEMAND_DIR / "traffic_trips.routed.rou.xml"
TRIPS = DEMAND_DIR / "traffic_trips.rou.xml"

OUT_OSM = DATA_OUTPUT_DIR / "bus_routes_osm_edges.csv"
OUT_SIM = DATA_OUTPUT_DIR / "bus_routes_sim_edges.csv"
OUT_SIM_SUMMARY = DATA_OUTPUT_DIR / "bus_routes_sim_summary.csv"


def load_net_edges(net_path: Path) -> dict[str, str]:
    """OSM way id (no #suffix) -> list of SUMO edge ids sharing that prefix."""
    root = ET.parse(net_path).getroot()
    by_way: dict[str, list[str]] = defaultdict(list)
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        base = eid.split("#", 1)[0]
        if base.startswith("-"):
            base = base[1:]
        by_way[base].append(eid)
    return by_way


def osm_way_to_sumo_edges(way_id: str, by_way: dict[str, list[str]]) -> list[str]:
    return sorted(by_way.get(way_id, []))


def export_osm_bus_routes(by_way: dict[str, list[str]]) -> list[dict]:
    root = ET.parse(OSM).getroot()
    rows: list[dict] = []
    for rel in root.findall("relation"):
        tags = {t.get("k"): t.get("v") for t in rel.findall("tag")}
        if tags.get("route") != "bus":
            continue
        ref = tags.get("ref", "")
        name = tags.get("name", "")
        rel_id = rel.get("id", "")
        way_ids: list[str] = []
        for m in rel.findall("member"):
            if m.get("type") == "way" and m.get("ref"):
                way_ids.append(m.get("ref"))
        sumo_edges: list[str] = []
        missing_ways: list[str] = []
        for wid in way_ids:
            mapped = osm_way_to_sumo_edges(wid, by_way)
            if mapped:
                sumo_edges.extend(mapped)
            else:
                missing_ways.append(wid)
        # preserve order, dedupe
        seen: set[str] = set()
        ordered_edges: list[str] = []
        for wid in way_ids:
            for eid in osm_way_to_sumo_edges(wid, by_way):
                if eid not in seen:
                    seen.add(eid)
                    ordered_edges.append(eid)
        rows.append(
            {
                "route_ref": ref,
                "route_name": name,
                "relation_id": rel_id,
                "from": tags.get("from", ""),
                "to": tags.get("to", ""),
                "direction": tags.get("direction", ""),
                "osm_way_count": len(way_ids),
                "sumo_edge_count": len(ordered_edges),
                "missing_osm_way_count": len(missing_ways),
                "sumo_edges": " ".join(ordered_edges),
                "osm_ways": " ".join(way_ids),
                "missing_osm_ways": " ".join(missing_ways),
            }
        )
    rows.sort(key=lambda r: (not str(r["route_ref"]).isdigit(), str(r["route_ref"]), r["route_name"]))
    return rows


def export_sim_bus_routes() -> tuple[list[dict], list[dict]]:
    """Unique bus OD/movement routes from traffic_trips.routed.rou.xml."""
    root = ET.parse(ROUTED).getroot()
    by_pattern: dict[str, dict] = {}
    detail_rows: list[dict] = []

    for veh in root.findall("vehicle"):
        if veh.get("type") != "bus":
            continue
        vid = veh.get("id", "")
        route_el = veh.find("route")
        if route_el is None:
            continue
        edges = route_el.get("edges", "").split()
        if not edges:
            continue
        # flow id = everything before last .N
        flow_id = re.sub(r"\.\d+$", "", vid)
        edge_str = " ".join(edges)
        if flow_id not in by_pattern:
            by_pattern[flow_id] = {
                "flow_id": flow_id,
                "example_vehicle_id": vid,
                "edge_count": len(edges),
                "from_edge": edges[0],
                "to_edge": edges[-1],
                "sumo_edges": edge_str,
            }
        detail_rows.append(
            {
                "flow_id": flow_id,
                "vehicle_id": vid,
                "depart": veh.get("depart", ""),
                "edge_count": len(edges),
                "from_edge": edges[0],
                "to_edge": edges[-1],
                "sumo_edges": edge_str,
            }
        )

    summary = sorted(by_pattern.values(), key=lambda r: r["flow_id"])
    return summary, detail_rows


def main() -> None:
    if not NET.exists():
        raise SystemExit(f"Missing net: {NET}")
    by_way = load_net_edges(NET)

    osm_rows = export_osm_bus_routes(by_way)
    with OUT_OSM.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(osm_rows[0].keys()) if osm_rows else [])
        if osm_rows:
            w.writeheader()
            w.writerows(osm_rows)

    sim_summary, sim_detail = export_sim_bus_routes()
    with OUT_SIM_SUMMARY.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(sim_summary[0].keys()) if sim_summary else [])
        if sim_summary:
            w.writeheader()
            w.writerows(sim_summary)
    with OUT_SIM.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(sim_detail[0].keys()) if sim_detail else [])
        if sim_detail:
            w.writeheader()
            w.writerows(sim_detail)

    print(f"OSM Metro bus route relations: {len(osm_rows)}")
    print(f"  -> {OUT_OSM.name}")
    refs = sorted({r["route_ref"] for r in osm_rows}, key=lambda x: (not str(x).isdigit(), str(x)))
    print(f"  Unique route refs: {len(refs)} -> {refs}")
    print()
    print(f"Simulation bus movement patterns (unique flows): {len(sim_summary)}")
    print(f"  -> {OUT_SIM_SUMMARY.name}")
    print(f"Simulation bus vehicles (all departures): {len(sim_detail)}")
    print(f"  -> {OUT_SIM.name}")


if __name__ == "__main__":
    main()
