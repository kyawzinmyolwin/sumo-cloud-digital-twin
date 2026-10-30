"""
Build bus demand from Metro OSM route relations (geometry + headway intervals).

Ignores survey ``other_vehicles`` and ECan calibration. Uses OSM ``route=bus``
member ways mapped to the clipped SUMO network, with departures every OSM
``interval`` tag (default 30 min) over the sumocfg window. Each route is
extended to boundary ``dead_end`` source/sink edges (same stubs as car demand)
along the full OSM route direction. Routes that pass through Bus Interchange keep the internal platform loop (same rules as
``sim_pipeline.apply_bus_interchange_to_route``).

Optional: pass ``--gtfs path/to/gtfs.zip`` (Metro API export) to use SUMO gtfs2pt
instead — requires SUMO_HOME, pandas, and rtree.

Usage:
  python create_timetable_buses.py
  python create_timetable_buses.py --write-routed
  python create_timetable_buses.py --merge-with-cars data/output/demand/traffic_trips.routed.rou.xml
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from _sim_root import DEMAND_DIR, NETWORK_DIR, SIM_ROOT

OSM = NETWORK_DIR / "Christchurch_Central_City.osm.xml"
NET = NETWORK_DIR / "Christchurch_Central_City_main_streets.net.xml"
SUMOCFG = SIM_ROOT / "Christchurch_Central_City_main_streets.sumocfg"

OUT_TRIPS = DEMAND_DIR / "traffic_trips_timetable_buses.rou.xml"
OUT_ROUTED = DEMAND_DIR / "traffic_trips_timetable_buses.routed.rou.xml"

SIM_BEGIN = 23400  # 06:30 — matches sumocfg
SIM_END = 86400
DEFAULT_HEADWAY_SEC = 1800
MIN_ROUTE_EDGES = 5
VCLASS = "bus"  # timetable routes must be drivable as bus on the patched net


@dataclass
class OsmBusRoute:
    relation_id: str
    ref: str
    name: str
    interval_sec: int
    from_place: str
    to_place: str
    way_ids: list[str]
    uses_platform_l: bool = False
    uses_platform_abcd: bool = False


def parse_sumocfg_times(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return SIM_BEGIN, SIM_END
    root = ET.parse(path).getroot()
    begin = SIM_BEGIN
    end = SIM_END
    for tag in root.iter("begin"):
        if tag.get("value"):
            begin = int(float(tag.get("value")))
    for tag in root.iter("end"):
        if tag.get("value"):
            end = int(float(tag.get("value")))
    return begin, end


def parse_interval(text: str, default: int = DEFAULT_HEADWAY_SEC) -> int:
    text = (text or "").strip()
    if not text:
        return default
    parts = text.split(":")
    try:
        if len(parts) == 3:
            h, m, s = (int(p) for p in parts)
            sec = h * 3600 + m * 60 + s
            return sec if sec > 0 else default
        if len(parts) == 2:
            m, s = (int(p) for p in parts)
            sec = m * 60 + s
            return sec if sec > 0 else default
    except ValueError:
        pass
    return default


PLATFORM_L_OSM_PLATFORM_NODE = "12181671814"
# Normal BI service uses platforms A–D (route 3 → A/C, route 8 → B/D). Platform L is
# Lichfield bay for off-hours / City Direct (84 & 85) only.
PLATFORM_ABCD_ROUTE_REFS = frozenset({"3", "8"})
PLATFORM_L_ROUTE_REFS = frozenset({"84", "85"})
# Bus Interchange platforms A–D (inside the building, not Lichfield Platform L).
PLATFORM_ABCD_OSM_NODES = frozenset(
    {"11164143160", "11164143161", "11164143162", "11164143163"}
)


def _load_platform_abcd_relation_ids(osm_path: Path) -> set[str]:
    root = ET.parse(osm_path).getroot()
    out: set[str] = set()
    for rel in root.findall("relation"):
        tags = {t.get("k"): t.get("v") for t in rel.findall("tag")}
        if tags.get("route") != "bus":
            continue
        if any(m.get("ref") in PLATFORM_ABCD_OSM_NODES for m in rel.findall("member")):
            out.add(rel.get("id") or "")
    return out


def _load_platform_l_relation_ids(osm_path: Path) -> set[str]:
    root = ET.parse(osm_path).getroot()
    out: set[str] = set()
    for rel in root.findall("relation"):
        tags = {t.get("k"): t.get("v") for t in rel.findall("tag")}
        if tags.get("route") != "bus":
            continue
        if any(
            m.get("ref") == PLATFORM_L_OSM_PLATFORM_NODE for m in rel.findall("member")
        ):
            out.add(rel.get("id") or "")
    return out


def load_osm_bus_routes(osm_path: Path) -> list[OsmBusRoute]:
    root = ET.parse(osm_path).getroot()
    platform_l_rel_ids = _load_platform_l_relation_ids(osm_path)
    platform_abcd_rel_ids = _load_platform_abcd_relation_ids(osm_path)
    routes: list[OsmBusRoute] = []
    for rel in root.findall("relation"):
        tags = {t.get("k"): t.get("v") for t in rel.findall("tag")}
        if tags.get("route") != "bus":
            continue
        if tags.get("network") not in (None, "", "Metro Christchurch"):
            continue
        ref = (tags.get("ref") or "").strip()
        if ref.upper().startswith("IC") or ref == "IC9557":
            continue  # skip intercity
        ways = [
            m.get("ref")
            for m in rel.findall("member")
            if m.get("type") == "way" and m.get("ref")
        ]
        rel_id = rel.get("id") or ""
        uses_platform_l = ref not in PLATFORM_ABCD_ROUTE_REFS and (
            ref in PLATFORM_L_ROUTE_REFS or rel_id in platform_l_rel_ids
        )
        from_place = (tags.get("from") or "").strip()
        place_l = from_place.lower()
        city_depart = place_l == "city" or place_l.endswith("/city") or "city/" in place_l
        uses_platform_abcd = ref in PLATFORM_ABCD_ROUTE_REFS or (
            not uses_platform_l
            and (rel_id in platform_abcd_rel_ids or not city_depart)
        )
        routes.append(
            OsmBusRoute(
                relation_id=rel_id,
                ref=ref or "unknown",
                name=(tags.get("name") or ref or "bus").strip(),
                interval_sec=parse_interval(tags.get("interval", "")),
                from_place=from_place,
                to_place=(tags.get("to") or "").strip(),
                way_ids=ways,
                uses_platform_l=uses_platform_l,
                uses_platform_abcd=uses_platform_abcd,
            )
        )
    return routes


def _osm_way_base(edge_id: str) -> str:
    base = edge_id[1:] if edge_id.startswith("-") else edge_id
    return base.split("#", 1)[0]


def edges_for_way(net, way_id: str) -> list[str]:
    out: list[str] = []
    for edge in net.getEdges():
        eid = edge.getID()
        if eid.startswith(":"):
            continue
        if _osm_way_base(eid) != way_id:
            continue
        if way_id in {"23151049"} and eid.startswith("-"):
            # OSM busway=opposite_lane artifact; use Tuam St Bus Lane / forward Tuam.
            continue
        out.append(eid)
    return sorted(out)


def _merge_paths(prefix: list[str], suffix: list[str]) -> list[str]:
    if not prefix:
        return list(suffix)
    if not suffix:
        return list(prefix)
    if prefix[-1] == suffix[0]:
        return prefix + suffix[1:]
    return prefix + suffix


def shortest_path(net, fr: str, to: str, vclass: str = VCLASS) -> list[str]:
    from sim_pipeline import shortest_vclass_edge_path

    return shortest_vclass_edge_path(net, fr, to, vclass, max_seen=12000, max_hops=200)


def pick_edge_for_way(net, way_id: str, prev_edge: str | None) -> str | None:
    cands = edges_for_way(net, way_id)
    if not cands:
        return None
    if not prev_edge:
        return cands[0]
    best_path: list[str] | None = None
    best_edge: str | None = None
    for cand in cands:
        path = shortest_path(net, prev_edge, cand)
        if path and (best_path is None or len(path) < len(best_path)):
            best_path = path
            best_edge = cand
    return best_edge or cands[0]


def _dedupe_edges(edges: list[str]) -> list[str]:
    out: list[str] = []
    for eid in edges:
        if not out or out[-1] != eid:
            out.append(eid)
    return out


def _sumo_reverse_edge(a: str, b: str) -> bool:
    """True when ``b`` is the opposite-direction SUMO edge of ``a`` (not split-way # fragments)."""
    if a == b:
        return False
    neg = b[1:] if b.startswith("-") else f"-{b}"
    return a == neg


def strip_reverse_uturns(edges: list[str]) -> list[str]:
    """Remove immediate back-and-forth on the same SUMO edge (e.g. ``-E`` then ``E``)."""
    edges = _dedupe_edges(edges)
    changed = True
    while changed:
        changed = False
        out: list[str] = []
        for eid in edges:
            if out and _sumo_reverse_edge(out[-1], eid):
                out.pop()
                changed = True
                continue
            if not out or out[-1] != eid:
                out.append(eid)
        edges = out
    return edges


def remove_edge_loops(
    net,
    edges: list[str],
    vclass: str = VCLASS,
    *,
    protected: set[str] | None = None,
) -> list[str]:
    """Drop stitched subcycles when the route revisits the same edge.

    Loops that run through Bus Interchange internal roads are kept — buses must
    use that loop rather than a banned portal shortcut.
    """
    from sim_pipeline import bus_interchange_internal_edges, edge_connected_vclass

    if protected is None:
        protected = bus_interchange_internal_edges(net)

    out: list[str] = []
    for eid in edges:
        if eid in out:
            idx = out.index(eid)
            loop_body = out[idx + 1 :]
            if eid in protected or any(e in protected for e in loop_body):
                if not out or out[-1] != eid:
                    out.append(eid)
                continue
            if edge_connected_vclass(net, out[idx], eid, vclass):
                out = out[: idx + 1]
                continue
        if not out or out[-1] != eid:
            out.append(eid)
    return out


# OSM: Bus Interchange Platform L on westbound Lichfield (-392044388#1).
LICHFIELD_PLATFORM_EDGE = "-392044388#1"
LICHFIELD_WB_EDGE = "-392044388#1"
COLOMBO_SB_DEPART_EDGE = "114648656#1"
# Platform L — Manchester approach and Colombo southbound exit (no internal BI loop).
PLATFORM_L_ENTRANCE: tuple[str, ...] = (
    "-436514739#2",
    "-436514739#1",
    "-436514739#0",
    "-1228958071",
    "-1015728523",
    "-777634281",
    LICHFIELD_PLATFORM_EDGE,
)
PLATFORM_L_EXIT: tuple[str, ...] = (
    LICHFIELD_PLATFORM_EDGE,
    COLOMBO_SB_DEPART_EDGE,
    "1015728524",
    "1015728525#0",
)
PLATFORM_L_THROUGH: tuple[str, ...] = PLATFORM_L_ENTRANCE + PLATFORM_L_EXIT[1:]
# Internal BI service-road exit (through routes only).
BI_COLOMBO_SERVICE_EDGE = "508372184"
BI_LICHFIELD_PORTAL_EDGE = "392044395"
BI_INTERNAL_BASES = frozenset(
    {
        "369800174",
        "508372189",
        "508372186",
        "506014262",
        "369800170",
        "369800173",
    }
)
PLATFORM_L_ZONE_BASES = BI_INTERNAL_BASES | frozenset(
    {
        "392044388",
        "392044395",
        "369800170",
        "369800173",
        "777634281",
        "1015728523",
        "1228958071",
        "436514739",
        "114648656",
        "1015728524",
        "1015728525",
    }
)
# Platforms A–D inside Bus Interchange (Tuam / Lichfield portals + internal loop).
PLATFORM_ABCD_TUAM_ENTER: tuple[str, ...] = ("993201434#1", "369800170#0")
PLATFORM_ABCD_TUAM_EXIT: tuple[str, ...] = ("508372184", "392044390#0")
PLATFORM_ABCD_LICH_ENTER: tuple[str, ...] = ("-1015728523", "-369800173#0")
PLATFORM_ABCD_LICH_EXIT: tuple[str, ...] = ("369800173#0", "1015728523")
PLATFORM_ABCD_TUAM_TUAM_VISIT: tuple[str, ...] = (
    *PLATFORM_ABCD_TUAM_ENTER,
    "369800170#1",
    "508372189",
    "506014262",
    "508372184",
    "392044390#0",
)
TUAM_SIDE_BASES = frozenset(
    {
        "993201434",
        "993201433",
        "392044390",
        "1056138897",
        "436515354",
        "1056138896",
    }
)
LICHFIELD_SIDE_BASES = frozenset(
    {
        "1015728523",
        "1228958071",
        "392044395",
        "777634281",
    }
)


def route_departs_city(osm_route: OsmBusRoute) -> bool:
    """Metro routes that start at Bus Interchange / City."""
    place = (osm_route.from_place or "").strip().lower()
    return place == "city" or place.endswith("/city") or "city/" in place


def _has_colombo_left_depart(edges: list[str]) -> bool:
    """True when the route turns left from westbound Lichfield onto southbound Colombo."""
    for a, b in zip(edges, edges[1:]):
        if a == LICHFIELD_WB_EDGE and b == COLOMBO_SB_DEPART_EDGE:
            return True
    return False


def _skip_bi_internal_segment(edges: list[str], start: int) -> int:
    """Index after a run of BI internal / service-road edges."""
    idx = start
    skip_bases = BI_INTERNAL_BASES | {BI_LICHFIELD_PORTAL_EDGE, BI_COLOMBO_SERVICE_EDGE}
    while idx < len(edges) and _osm_way_base(edges[idx]) in skip_bases:
        idx += 1
    return idx


def _dedupe_consecutive_edges(edges: list[str]) -> list[str]:
    out: list[str] = []
    for eid in edges:
        if not out or out[-1] != eid:
            out.append(eid)
    return out


def _lichfield_approach_via_platform(net, from_edge: str) -> list[str]:
    """Hop from from_edge via the Lichfield platform bay to westbound Lichfield."""
    from sim_pipeline import shortest_vclass_edge_path

    if from_edge == LICHFIELD_WB_EDGE:
        return []
    if from_edge == LICHFIELD_PLATFORM_EDGE:
        hop = shortest_vclass_edge_path(
            net, LICHFIELD_PLATFORM_EDGE, LICHFIELD_WB_EDGE, "bus", allow_uturn=False
        )
        return hop[1:] if hop and len(hop) >= 2 else []

    hop = shortest_vclass_edge_path(
        net, from_edge, LICHFIELD_WB_EDGE, "bus", allow_uturn=False
    )
    if not hop or len(hop) < 2:
        return []
    body = hop[1:]
    if LICHFIELD_PLATFORM_EDGE in body:
        return body

    to_plat = shortest_vclass_edge_path(
        net, from_edge, LICHFIELD_PLATFORM_EDGE, "bus", allow_uturn=False
    )
    plat_wb = shortest_vclass_edge_path(
        net, LICHFIELD_PLATFORM_EDGE, LICHFIELD_WB_EDGE, "bus", allow_uturn=False
    )
    if to_plat and plat_wb and len(to_plat) >= 2 and len(plat_wb) >= 2:
        return to_plat[1:] + plat_wb[1:]
    return body


def _ensure_lichfield_platform_before_wb(edges: list[str]) -> list[str]:
    """Insert the platform edge immediately before westbound Lichfield when missing."""
    out: list[str] = []
    for eid in edges:
        if (
            eid == LICHFIELD_WB_EDGE
            and out
            and out[-1] != LICHFIELD_PLATFORM_EDGE
            and LICHFIELD_PLATFORM_EDGE not in out[-3:]
        ):
            out.append(LICHFIELD_PLATFORM_EDGE)
        out.append(eid)
    return _dedupe_consecutive_edges(out)


def enforce_city_depart_colombo_left(net, edges: list[str]) -> list[str]:
    """
    City departures: westbound Lichfield (-392044388#1) then left onto Colombo (114648656#1).

    Replaces BI internal portal exits (392044395 -> 508372184) and Lichfield U-turns.
    """
    from sim_pipeline import edge_connected_vclass, shortest_vclass_edge_path

    if _has_colombo_left_depart(edges):
        return _ensure_lichfield_platform_before_wb(edges)

    out = list(edges)
    cut_start: int | None = None
    for i, eid in enumerate(out):
        if eid == BI_LICHFIELD_PORTAL_EDGE:
            cut_start = i
            break
        if eid == "-369800173#0" or _osm_way_base(eid) in BI_INTERNAL_BASES:
            cut_start = i
            break

    if cut_start is None:
        for i, eid in enumerate(out):
            if eid in ("-1015728523", "1015728523") and i + 1 < len(out):
                nxt = out[i + 1]
                if _osm_way_base(nxt) in BI_INTERNAL_BASES or nxt == BI_LICHFIELD_PORTAL_EDGE:
                    cut_start = i + 1
                    break
        if cut_start is None:
            for i in range(len(out) - 1, -1, -1):
                if out[i] in ("-1015728523", "1015728523"):
                    cut_start = i + 1
                    break

    if cut_start is None:
        return out

    prefix_end = cut_start
    while prefix_end > 0 and _osm_way_base(out[prefix_end - 1]) in BI_INTERNAL_BASES:
        prefix_end -= 1
    prefix = out[:prefix_end]
    if not prefix:
        return out

    tail_start = _skip_bi_internal_segment(out, cut_start)
    tail = out[tail_start:]
    anchor = prefix[-1]
    approach = _lichfield_approach_via_platform(net, anchor)
    if approach and approach[-1] != LICHFIELD_WB_EDGE:
        return out
    if not approach and anchor != LICHFIELD_WB_EDGE:
        return out

    rebuilt_prefix = prefix + approach
    mid = (
        [COLOMBO_SB_DEPART_EDGE]
        if rebuilt_prefix and rebuilt_prefix[-1] == LICHFIELD_WB_EDGE
        else [LICHFIELD_WB_EDGE, COLOMBO_SB_DEPART_EDGE]
    )

    if tail and edge_connected_vclass(net, COLOMBO_SB_DEPART_EDGE, tail[0], "bus"):
        candidate = rebuilt_prefix + mid + tail
    elif out:
        hop = shortest_vclass_edge_path(
            net, COLOMBO_SB_DEPART_EDGE, out[-1], "bus", allow_uturn=False
        )
        candidate = rebuilt_prefix + mid + (hop[1:] if hop and len(hop) >= 2 else tail)
    else:
        candidate = rebuilt_prefix + mid

    candidate = _ensure_lichfield_platform_before_wb(candidate)
    if route_is_bus_valid(net, candidate) and _has_colombo_left_depart(candidate):
        return candidate
    return out


def _route_has_lichfield_uturn_after_bi(edges: list[str]) -> bool:
    """True when a bus exits BI internal roads back onto eastbound Lichfield."""
    for i in range(1, len(edges) - 1):
        if edges[i] == "369800173#0" and edges[i + 1] == "1015728523":
            if i + 2 < len(edges) and edges[i : i + 2] == list(PLATFORM_ABCD_LICH_EXIT):
                continue  # official Platform A–D exit to Lichfield
            prev_base = _osm_way_base(edges[i - 1])
            if prev_base in {"508372186", "508372189", "369800174", "506014262"}:
                return True
    return False


def fix_bi_lichfield_uturn_exit(
    net, edges: list[str], *, depart_city: bool = False
) -> list[str]:
    """
    Replace BI exits that U-turn back onto eastbound Lichfield (1015728523).

    Through routes use internal Colombo (508372184); City departures use the public
    left turn ``-392044388#1`` -> ``114648656#1``.
    """
    from sim_pipeline import edge_connected_vclass, shortest_vclass_edge_path

    if depart_city:
        return enforce_city_depart_colombo_left(net, edges)

    out = list(edges)
    dest = out[-1] if out else ""
    service_mid = ["506014262", BI_COLOMBO_SERVICE_EDGE]
    changed = True
    while changed:
        changed = False
        for i in range(len(out) - 2):
            if out[i] != "508372186" or out[i + 1] != "369800173#0" or out[i + 2] != "1015728523":
                continue
            prefix = out[:i]
            tail = out[i + 3 :]
            mid = service_mid if prefix and prefix[-1] == "508372189" else [BI_LICHFIELD_PORTAL_EDGE, BI_COLOMBO_SERVICE_EDGE]
            candidate: list[str] | None = None
            if tail and edge_connected_vclass(net, mid[-1], tail[0], "bus"):
                candidate = prefix + mid + tail
            elif dest:
                hop = shortest_vclass_edge_path(
                    net, BI_COLOMBO_SERVICE_EDGE, dest, "bus", allow_uturn=False
                )
                if hop and len(hop) >= 2:
                    candidate = prefix + mid + hop[1:]
            if candidate and route_is_bus_valid(net, candidate):
                out = candidate
                dest = out[-1]
                changed = True
                break

        if changed:
            continue

        for i in range(len(out) - 2):
            if out[i] != BI_LICHFIELD_PORTAL_EDGE:
                continue
            for j in range(i + 1, len(out) - 1):
                if out[j] != "369800173#0" or out[j + 1] != "1015728523":
                    continue
                prefix = out[:i]
                tail = out[j + 2 :]
                mid = [BI_LICHFIELD_PORTAL_EDGE, BI_COLOMBO_SERVICE_EDGE]
                candidate = None
                if tail and edge_connected_vclass(net, BI_COLOMBO_SERVICE_EDGE, tail[0], "bus"):
                    candidate = prefix + mid + tail
                elif dest:
                    hop = shortest_vclass_edge_path(
                        net, BI_COLOMBO_SERVICE_EDGE, dest, "bus", allow_uturn=False
                    )
                    if hop and len(hop) >= 2:
                        candidate = prefix + mid + hop[1:]
                if candidate and route_is_bus_valid(net, candidate):
                    out = candidate
                    dest = out[-1]
                    changed = True
                    break
            if changed:
                break
    return out


def _index_of_subsequence(edges: list[str], sub: tuple[str, ...] | list[str]) -> int | None:
    needle = list(sub)
    n = len(needle)
    if n == 0:
        return None
    for i in range(len(edges) - n + 1):
        if edges[i : i + n] == needle:
            return i
    return None


def _has_platform_l_corridor(edges: list[str]) -> bool:
    return _index_of_subsequence(edges, PLATFORM_L_THROUGH) is not None


def _edge_in_platform_l_zone(eid: str) -> bool:
    return _osm_way_base(eid) in PLATFORM_L_ZONE_BASES


def _platform_l_zone_span(edges: list[str]) -> tuple[int | None, int | None]:
    """First/last index of BI / Lichfield / Colombo interchange edges to replace."""
    start: int | None = None
    end: int | None = None
    for i, eid in enumerate(edges):
        if _edge_in_platform_l_zone(eid):
            if start is None:
                start = i
            end = i
    return start, end


def _connect_edge_paths(net, prefix: list[str], corridor: list[str], suffix: list[str]) -> list[str]:
    """Join prefix, fixed corridor, and suffix with bus-valid hops when needed."""
    from sim_pipeline import edge_connected_vclass, shortest_vclass_edge_path

    chunks: list[list[str]] = []
    if prefix:
        chunks.append(prefix)
    if prefix and corridor:
        if not edge_connected_vclass(net, prefix[-1], corridor[0], "bus"):
            hop = shortest_vclass_edge_path(
                net, prefix[-1], corridor[0], "bus", allow_uturn=False
            )
            if not hop or len(hop) < 2:
                return []
            chunks.append(hop[1:])
    chunks.append(corridor)
    if suffix:
        if corridor and not edge_connected_vclass(net, corridor[-1], suffix[0], "bus"):
            hop = shortest_vclass_edge_path(
                net, corridor[-1], suffix[0], "bus", allow_uturn=False
            )
            if not hop or len(hop) < 2:
                return []
            chunks.append(hop[1:])
        chunks.append(suffix)
    out: list[str] = []
    for part in chunks:
        out = _merge_paths(out, part) if out else list(part)
    return _dedupe_consecutive_edges(out)


def _platform_l_corridor_for_visit(prefix: list[str], suffix: list[str]) -> tuple[str, ...]:
    if prefix and suffix:
        return PLATFORM_L_THROUGH
    if suffix:
        return PLATFORM_L_EXIT
    if prefix:
        return PLATFORM_L_ENTRANCE
    return PLATFORM_L_THROUGH


def _strip_bi_internals_for_platform_l(net, edges: list[str]) -> list[str]:
    """Drop internal interchange loops; keep the public Platform L corridor only."""
    from sim_pipeline import repair_bus_block_turns

    if not _has_platform_l_corridor(edges):
        return edges
    skip_bases = BI_INTERNAL_BASES | frozenset({"369800170"})
    skip_edges = {BI_LICHFIELD_PORTAL_EDGE, BI_COLOMBO_SERVICE_EDGE}
    filtered = [
        e
        for e in edges
        if _osm_way_base(e) not in skip_bases and e not in skip_edges
    ]
    filtered = _dedupe_consecutive_edges(filtered)
    repaired = repair_bus_block_turns(net, filtered, "bus")
    if (
        repaired
        and route_is_bus_valid(net, repaired)
        and _has_platform_l_corridor(repaired)
    ):
        return repaired
    return edges


def enforce_platform_l_corridor(net, edges: list[str]) -> list[str]:
    """
    Platform L: Manchester entrance and Colombo exit — not the internal BI loop.

    Entrance: -436514739#2 … -392044388#1 (Platform L)
    Exit:     -392044388#1 … 1015728525#0
    """
    if _has_platform_l_corridor(edges):
        return _strip_bi_internals_for_platform_l(net, _dedupe_consecutive_edges(edges))

    start, end = _platform_l_zone_span(edges)
    if start is None:
        return edges

    prefix = edges[:start]
    suffix = edges[end + 1 :]
    while suffix and (
        _osm_way_base(suffix[0]) in BI_INTERNAL_BASES
        or suffix[0] in (BI_LICHFIELD_PORTAL_EDGE, BI_COLOMBO_SERVICE_EDGE)
    ):
        suffix = suffix[1:]
    corridor = list(_platform_l_corridor_for_visit(prefix, suffix))
    candidate = _connect_edge_paths(net, prefix, corridor, suffix)
    if candidate and route_is_bus_valid(net, candidate):
        if _index_of_subsequence(candidate, corridor) is not None:
            return _strip_bi_internals_for_platform_l(net, candidate)
    fallback = _dedupe_consecutive_edges(prefix + corridor + suffix)
    if route_is_bus_valid(net, fallback):
        return _strip_bi_internals_for_platform_l(net, fallback)
    return edges


def _portal_side_from_edges(edges: list[str]) -> str:
    """Guess Tuam vs Lichfield side from edge bases along an approach or departure leg."""
    tuam = sum(1 for e in edges if _osm_way_base(e) in TUAM_SIDE_BASES)
    lich = sum(1 for e in edges if _osm_way_base(e) in LICHFIELD_SIDE_BASES)
    return "tuam" if tuam >= lich else "lichfield"


def _osm_portal_sides(osm_route: OsmBusRoute) -> tuple[str, str]:
    """Infer BI approach/departure side from OSM member-way order (Tuam vs Lichfield)."""
    tuam_pos: int | None = None
    lich_pos: int | None = None
    for i, way_id in enumerate(osm_route.way_ids):
        if way_id in TUAM_SIDE_BASES and tuam_pos is None:
            tuam_pos = i
        if way_id in LICHFIELD_SIDE_BASES and lich_pos is None:
            lich_pos = i
    if tuam_pos is not None and lich_pos is not None:
        if tuam_pos < lich_pos:
            return "tuam", "lichfield"
        return "lichfield", "tuam"
    if tuam_pos is not None:
        return "tuam", "tuam"
    if lich_pos is not None:
        return "lichfield", "lichfield"
    return "tuam", "tuam"


def _build_platform_abcd_visit(net, side_in: str, side_out: str) -> list[str] | None:
    """Official BI portal legs + internal platform loop between A–D bays."""
    from sim_pipeline import (
        _bus_interchange_portal_route_options,
        _visit_includes_platform_loop,
        bus_interchange_internal_edges,
    )

    if side_in == "tuam" and side_out == "tuam":
        return list(PLATFORM_ABCD_TUAM_TUAM_VISIT)

    enter_end = {"tuam": PLATFORM_ABCD_TUAM_ENTER[-1], "lichfield": PLATFORM_ABCD_LICH_ENTER[-1]}[
        side_in
    ]
    exit_start = {"tuam": PLATFORM_ABCD_TUAM_EXIT[0], "lichfield": PLATFORM_ABCD_LICH_EXIT[0]}[
        side_out
    ]
    enter = PLATFORM_ABCD_TUAM_ENTER if side_in == "tuam" else PLATFORM_ABCD_LICH_ENTER
    exit_ = PLATFORM_ABCD_TUAM_EXIT if side_out == "tuam" else PLATFORM_ABCD_LICH_EXIT

    internal = bus_interchange_internal_edges(net)
    for opt in _bus_interchange_portal_route_options(net, internal):
        if not _visit_includes_platform_loop(opt):
            continue
        if opt[0] == enter_end and opt[-1] == exit_start:
            return list(enter) + list(opt[1:]) + list(exit_[1:])
    return None


def _has_platform_abcd_portals(
    edges: list[str],
    side_in: str | None = None,
    side_out: str | None = None,
) -> bool:
    """True when route uses the required Tuam/Lichfield portal edge pairs."""
    if side_in is not None and side_out is not None:
        enter = (
            PLATFORM_ABCD_TUAM_ENTER
            if side_in == "tuam"
            else PLATFORM_ABCD_LICH_ENTER
        )
        exit_ = (
            PLATFORM_ABCD_TUAM_EXIT
            if side_out == "tuam"
            else PLATFORM_ABCD_LICH_EXIT
        )
        ei = _index_of_subsequence(edges, enter)
        if ei is None:
            return False
        return _index_of_subsequence(edges[ei:], exit_) is not None

    combos = (
        (PLATFORM_ABCD_TUAM_ENTER, PLATFORM_ABCD_TUAM_EXIT),
        (PLATFORM_ABCD_LICH_ENTER, PLATFORM_ABCD_LICH_EXIT),
        (PLATFORM_ABCD_TUAM_ENTER, PLATFORM_ABCD_LICH_EXIT),
        (PLATFORM_ABCD_LICH_ENTER, PLATFORM_ABCD_TUAM_EXIT),
    )
    for enter, exit_ in combos:
        ei = _index_of_subsequence(edges, enter)
        if ei is None:
            continue
        if _index_of_subsequence(edges[ei:], exit_) is not None:
            return True
    return False


def _platform_abcd_zone_span(edges: list[str]) -> tuple[int | None, int | None]:
    start: int | None = None
    end: int | None = None
    for i, eid in enumerate(edges):
        if _edge_in_platform_l_zone(eid):
            if start is None:
                start = i
            end = i
    return start, end


def enforce_platform_abcd_portals(
    net, edges: list[str], osm_route: OsmBusRoute | None = None
) -> list[str]:
    """
    Platforms A–D: splice official Tuam / Lichfield portals and internal loop.

    - From Tuam: 993201434#1 → 369800170#0 … exit 508372184 → 392044390#0
    - From Lichfield: -1015728523 → -369800173#0 … exit 369800173#0 → 1015728523
    """
    if osm_route is not None:
        side_in, side_out = _osm_portal_sides(osm_route)
    else:
        side_in = _portal_side_from_edges(edges[:8])
        side_out = side_in

    if _has_platform_abcd_portals(edges, side_in, side_out):
        return _dedupe_consecutive_edges(edges)

    start, end = _platform_abcd_zone_span(edges)
    if start is None:
        return edges

    prefix = edges[:start]
    suffix = edges[end + 1 :]
    while suffix and (
        _osm_way_base(suffix[0]) in BI_INTERNAL_BASES
        or suffix[0] in (BI_LICHFIELD_PORTAL_EDGE, BI_COLOMBO_SERVICE_EDGE)
    ):
        suffix = suffix[1:]

    visit = _build_platform_abcd_visit(net, side_in, side_out)
    if not visit:
        return edges

    candidate = _connect_edge_paths(net, prefix, visit, suffix)
    if candidate and route_is_bus_valid(net, candidate) and _has_platform_abcd_portals(
        candidate, side_in, side_out
    ):
        return candidate

    fallback = _dedupe_consecutive_edges(prefix + visit + suffix)
    if route_is_bus_valid(net, fallback) and _has_platform_abcd_portals(
        fallback, side_in, side_out
    ):
        return fallback
    return edges


def apply_timetable_bus_interchange(
    net, edges: list[str], osm_route: OsmBusRoute
) -> list[str]:
    """Apply BI rules: platform loop for through routes; Colombo left for City depart."""
    from sim_pipeline import apply_bus_interchange_to_route

    if not edges:
        return edges
    if osm_route.uses_platform_l:
        return enforce_platform_l_corridor(net, edges)
    if osm_route.uses_platform_abcd:
        return enforce_platform_abcd_portals(net, edges, osm_route)

    depart_city = route_departs_city(osm_route)
    edges = apply_bus_interchange_to_route(
        net,
        edges[0],
        edges[-1],
        edges,
        require_platform_loop=not depart_city,
    )
    edges = fix_bi_lichfield_uturn_exit(net, edges, depart_city=depart_city)
    if depart_city:
        edges = enforce_city_depart_colombo_left(net, edges)
    return edges


def _clean_stitched_route(net, edges: list[str]) -> list[str]:
    edges = strip_reverse_uturns(edges)
    edges = remove_edge_loops(net, edges)
    return strip_reverse_uturns(edges)


def finalize_bus_route(net, edges: list[str]) -> list[str]:
    """
    Remove OSM-stitch U-turns and loops while keeping a bus-valid path.

    Tries progressively lighter cleaning; keeps the original route if cleaning
    would break connectivity (split OSM ways like ``way#0`` → ``way#1`` are kept).
    """
    from sim_pipeline import (
        _redirect_saint_asaph_straight_to_bus_lane,
        _repair_cbd_bus_corridors,
        _repair_tuam_bus_lane_forbidden_exits,
    )

    edges = _redirect_saint_asaph_straight_to_bus_lane(net, edges, "bus")
    edges = _repair_cbd_bus_corridors(net, edges, "bus")
    edges = _repair_tuam_bus_lane_forbidden_exits(net, edges, "bus")
    candidates = (
        _clean_stitched_route(net, edges),
        remove_edge_loops(net, strip_reverse_uturns(edges)),
        strip_reverse_uturns(edges),
        edges,
    )
    for cand in candidates:
        if len(cand) < MIN_ROUTE_EDGES:
            continue
        if _route_has_lichfield_uturn_after_bi(cand):
            continue
        if route_is_bus_valid(net, cand):
            return cand
    return edges


def build_sumo_route(net, way_ids: list[str]) -> list[str] | None:
    """
    Map OSM ways to SUMO edges inside the clipped network.

    Long Metro lines mostly run outside the clip; keep the longest in-network
    contiguous segment (usually the CBD / Bus Interchange corridor).
    """
    anchors: list[str] = []
    for way_id in way_ids:
        prev = anchors[-1] if anchors else None
        edge = pick_edge_for_way(net, way_id, prev)
        if edge is None:
            continue
        if not anchors or anchors[-1] != edge:
            anchors.append(edge)

    if len(anchors) < MIN_ROUTE_EDGES:
        return None

    segments: list[list[str]] = []
    chain = [anchors[0]]
    for nxt in anchors[1:]:
        if chain[-1] == nxt:
            continue
        hop = shortest_path(net, chain[-1], nxt)
        if hop and len(hop) >= 2:
            chain = _merge_paths(chain, hop[1:])
        else:
            compact = _dedupe_edges(chain)
            if len(compact) >= MIN_ROUTE_EDGES:
                segments.append(compact)
            chain = [nxt]
    compact = _dedupe_edges(chain)
    if len(compact) >= MIN_ROUTE_EDGES:
        segments.append(compact)

    if not segments:
        return None
    return max(segments, key=len)


def edge_in_net(net, edge_id: str) -> bool:
    try:
        net.getEdge(edge_id)
        return True
    except Exception:
        return False


def route_is_bus_valid(net, edges: list[str]) -> bool:
    from sim_pipeline import is_drivable_vclass, edge_connected_vclass

    if len(edges) < MIN_ROUTE_EDGES:
        return False
    for eid in edges:
        if not edge_in_net(net, eid):
            return False
    if not is_drivable_vclass(net.getEdge(edges[0]), "bus"):
        return False
    for a, b in zip(edges, edges[1:]):
        if not edge_connected_vclass(net, a, b, "bus"):
            return False
    return True


def safe_id(*parts: str) -> str:
    raw = "_".join(p for p in parts if p)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:120]


def departures(headway: int, begin: int, end: int, offset: int) -> list[int]:
    t = begin + (offset % max(headway, 1))
    out: list[int] = []
    while t < end:
        out.append(t)
        t += headway
    return out


def write_timetable_routes(
    net,
    osm_routes: list[OsmBusRoute],
    out_path: Path,
    *,
    sim_begin: int,
    sim_end: int,
) -> tuple[int, int, list[str]]:
    root = ET.Element("routes")
    ET.SubElement(root, "vType", id="bus", vClass="bus", color="0,122,135")

    route_defs: list[tuple[OsmBusRoute, list[str], str]] = []
    skipped: list[str] = []

    for i, route in enumerate(osm_routes):
        edges = build_sumo_route(net, route.way_ids)
        if edges:
            edges = finalize_bus_route(net, edges)
            edges = apply_timetable_bus_interchange(net, edges, route)
            if len(edges) < MIN_ROUTE_EDGES:
                edges = None
        if route.uses_platform_l and edges:
            edges = enforce_platform_l_corridor(net, edges)
        elif route.uses_platform_abcd and edges:
            bi_start, _ = _platform_abcd_zone_span(edges)
            if bi_start is not None:
                edges = enforce_platform_abcd_portals(net, edges, route)
        elif route_departs_city(route) and edges and not _has_colombo_left_depart(edges):
            edges = enforce_city_depart_colombo_left(net, edges)
        if edges:
            from sim_pipeline import extend_route_to_boundary_dead_ends

            edges = extend_route_to_boundary_dead_ends(net, edges, VCLASS)
        if (
            not edges
            or not route_is_bus_valid(net, edges)
            or _route_has_lichfield_uturn_after_bi(edges)
            or (route.uses_platform_l and not _has_platform_l_corridor(edges))
            or (
                route_departs_city(route)
                and not route.uses_platform_l
                and not route.uses_platform_abcd
                and not _has_colombo_left_depart(edges)
            )
        ):
            skipped.append(f"{route.ref} {route.name} (no bus-valid path in net)")
            continue
        rid = safe_id("R", route.ref, route.from_place, route.to_place, route.relation_id)
        route_defs.append((route, edges, rid))
        ET.SubElement(root, "route", id=rid, edges=" ".join(edges))

    n_veh = 0
    depart_rows: list[tuple[float, ET.Element]] = []
    from sim_pipeline import SELWYN_BOUNDARY_SOURCE_EDGE

    for idx, (route, edges, rid) in enumerate(route_defs):
        departs = departures(route.interval_sec, sim_begin, sim_end, idx * 137)
        for j, depart in enumerate(departs):
            attrs: dict[str, str] = {
                "id": safe_id("TT", route.ref, rid, str(depart), str(j)),
                "type": "bus",
                "route": rid,
                "depart": f"{depart:.2f}",
            }
            if edges and edges[0] == SELWYN_BOUNDARY_SOURCE_EDGE:
                attrs["departLane"] = "0"
                attrs["departPos"] = "0"
            el = ET.Element("vehicle", **attrs)
            depart_rows.append((float(depart), el))
            n_veh += 1

    for _depart, el in sorted(depart_rows, key=lambda x: x[0]):
        root.append(el)

    if hasattr(ET, "indent"):
        ET.indent(root, space="    ")
    tree = ET.ElementTree(root)
    tree.write(out_path, encoding="UTF-8", xml_declaration=True)
    return len(route_defs), n_veh, skipped


def write_routed_copy(trips_path: Path, routed_path: Path) -> None:
    """Copy pre-validated timetable routes (duarouter would not improve them)."""
    import shutil

    shutil.copy2(trips_path, routed_path)


def _sort_routes_by_depart(root: ET.Element) -> None:
    """Reorder vehicle elements by depart time (vTypes and route defs stay first)."""
    head: list[ET.Element] = []
    vehicles: list[ET.Element] = []
    seen_head: set[str] = set()
    for el in list(root):
        if el.tag == "vehicle":
            vehicles.append(el)
            root.remove(el)
        elif el.tag == "vType":
            key = f"{el.tag}:{el.get('id', '')}"
            if key not in seen_head:
                seen_head.add(key)
                head.append(el)
            root.remove(el)
        elif el.tag == "route":
            rid = el.get("id", "")
            key = f"route:{rid}"
            if key in seen_head:
                head = [h for h in head if not (h.tag == "route" and h.get("id") == rid)]
            seen_head.add(key)
            head.append(el)
            root.remove(el)
        else:
            head.append(el)
            root.remove(el)

    vehicles.sort(
        key=lambda el: (float(el.get("depart", "0")), el.get("id", "")),
    )
    for el in head:
        root.append(el)
    for el in vehicles:
        root.append(el)


def merge_car_routes(car_routed: Path, bus_trips: Path, out_path: Path) -> None:
    """Keep cars from an existing routed file; replace all buses with timetable buses."""
    car_root = ET.parse(car_routed).getroot()
    bus_root = ET.parse(bus_trips).getroot()
    bus_route_ids = {
        el.get("id")
        for el in bus_root.findall("route")
        if el.get("id")
    }

    out = ET.Element("routes")
    seen_vtypes: set[str] = set()
    for el in car_root:
        if el.tag == "vType" and el.get("id") != "bus":
            vid = el.get("id")
            if vid not in seen_vtypes:
                seen_vtypes.add(vid)
                out.append(el)
    for el in bus_root:
        if el.tag == "vType":
            vid = el.get("id")
            if vid not in seen_vtypes:
                seen_vtypes.add(vid)
                out.append(el)

    for el in car_root:
        if el.tag == "vType":
            continue
        if el.tag == "route" and el.get("id") in bus_route_ids:
            continue
        if el.tag == "vehicle" and el.get("type") == "bus":
            continue
        out.append(el)

    for el in bus_root:
        if el.tag in ("route", "vehicle"):
            out.append(el)

    _sort_routes_by_depart(out)

    if hasattr(ET, "indent"):
        ET.indent(out, space="    ")
    ET.ElementTree(out).write(out_path, encoding="UTF-8", xml_declaration=True)

    from sim_pipeline import validate_repair_routed_routes

    checked, repaired, dropped = validate_repair_routed_routes(routed_path=out_path)
    if repaired or dropped:
        print(
            f"route validation after merge: checked {checked:,}, "
            f"repaired {repaired:,}, dropped {dropped:,}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Timetable-style buses from OSM Metro routes")
    p.add_argument("--osm", type=Path, default=OSM)
    p.add_argument("--net", type=Path, default=NET)
    p.add_argument("-o", "--output", type=Path, default=OUT_TRIPS)
    p.add_argument("--write-routed", action="store_true", help="Also write .routed.rou.xml")
    p.add_argument(
        "--merge-with-cars",
        type=Path,
        metavar="ROUTED",
        help="Write traffic_trips.routed.rou.xml = cars from ROUTED + timetable buses",
    )
    p.add_argument("--gtfs", type=Path, help="Optional Metro GTFS zip (uses SUMO gtfs2pt)")
    args = p.parse_args(argv)

    if args.gtfs:
        print(
            "GTFS mode: run SUMO gtfs2pt manually with your API key, e.g.\n"
            "  python $SUMO_HOME/tools/import/gtfs/gtfs2pt.py "
            f"-n {args.net} --gtfs {args.gtfs} --route-output {args.output}\n"
            "This script uses OSM route+interval data when --gtfs is not processed here."
        )
        return 0

    from sim_pipeline import setup_sumolib

    if setup_sumolib() is None:
        raise SystemExit("SUMO_HOME not found")
    import sumolib  # noqa: E402

    if not args.net.is_file():
        raise SystemExit(f"Missing network: {args.net}")
    if not args.osm.is_file():
        raise SystemExit(f"Missing OSM: {args.osm}")

    sim_begin, sim_end = parse_sumocfg_times(SUMOCFG)
    net = sumolib.net.readNet(str(args.net), withInternal=True)
    osm_routes = load_osm_bus_routes(args.osm)
    n_routes, n_veh, skipped = write_timetable_routes(
        net, osm_routes, args.output, sim_begin=sim_begin, sim_end=sim_end
    )

    print(f"OSM Metro bus relations read: {len(osm_routes)}")
    print(f"Routes mapped in network: {n_routes}")
    print(f"Timetable vehicles (headway departures): {n_veh}")
    print(f"Output: {args.output}")
    if skipped:
        print(f"Skipped ({len(skipped)}):")
        for line in skipped[:12]:
            print(f"  - {line}")
        if len(skipped) > 12:
            print(f"  ... and {len(skipped) - 12} more")

    if args.write_routed:
        write_routed_copy(args.output, OUT_ROUTED)
        print(f"Routed: {OUT_ROUTED}")

    if args.merge_with_cars:
        from sim_pipeline import OUT_ROUTED as DEFAULT_ROUTED

        dest = DEFAULT_ROUTED
        merge_car_routes(args.merge_with_cars, args.output, dest)
        print(f"Merged cars + timetable buses -> {dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
