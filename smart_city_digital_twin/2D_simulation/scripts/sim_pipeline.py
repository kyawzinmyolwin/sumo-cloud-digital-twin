"""
Christchurch Central City SUMO pipeline — shared library.

Entry points:
  create_network.py  — network, map
  create_demand.py   — trips, duarouter
  build_simulation.py — full pipeline (both scripts)
"""
from __future__ import annotations

import argparse
import copy
import csv
import glob
import os
import random
from concurrent.futures import ProcessPoolExecutor
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pipeline_progress import (
    PipelineStepProgress,
    TripsStepProgress,
    _fmt_duration,
    _tick_refresh_due,
    parse_duarouter_timestep,
    run_subprocess_with_progress,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_INPUT_DIR = DATA_DIR / "input"
DATA_OUTPUT_DIR = DATA_DIR / "output"
NETWORK_DIR = DATA_OUTPUT_DIR / "network"
DEMAND_DIR = DATA_OUTPUT_DIR / "demand"
DEFAULT_SUMO_HOME = Path(r"C:\Sumo")
COORD_SANITY_LIMIT = 1_000_000.0

# Windows may keep .net.xml locked briefly after netconvert / sumo-gui / IDE.
_REPLACE_NET_RETRIES = 12
_REPLACE_NET_DELAYS_S = (0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)


def replace_net_file(tmp: Path, net_path: Path) -> None:
    """Atomically replace net_path with tmp; retry when the target is still locked."""
    tmp = Path(tmp)
    net_path = Path(net_path)
    if not tmp.is_file():
        raise FileNotFoundError(f"missing netconvert output: {tmp}")

    last_err: OSError | None = None
    for delay in _REPLACE_NET_DELAYS_S[:_REPLACE_NET_RETRIES]:
        try:
            os.replace(tmp, net_path)
            return
        except OSError as e:
            winerr = getattr(e, "winerror", None)
            if e.errno not in (13, 26) and winerr not in (5, 32):
                raise
            last_err = e
        time.sleep(delay)

    # Last resort: backup swap (works when the lock is brief).
    bak = net_path.with_suffix(net_path.suffix + ".bak")
    try:
        if bak.is_file():
            bak.unlink()
        if net_path.is_file():
            os.replace(net_path, bak)
        os.replace(tmp, net_path)
        if bak.is_file():
            bak.unlink()
        return
    except OSError as e:
        last_err = e
        if bak.is_file() and not net_path.is_file():
            try:
                os.replace(bak, net_path)
            except OSError:
                pass

    if tmp.is_file():
        shutil.copy2(tmp, net_path.with_suffix(net_path.suffix + ".failed-replace.xml"))
    hint = (
        f"Could not replace {net_path} — the file is probably open elsewhere "
        f"(sumo-gui, another terminal running the pipeline, or the editor). "
        f"Close it and run create_network.py again. "
        f"Netconvert output kept as: {tmp.name}"
    )
    raise PermissionError(hint) from last_err

# Inputs
MAIN_STREETS = NETWORK_DIR / "main_streets.txt"
OSM_IN = NETWORK_DIR / "Christchurch_Central_City.osm.xml"
INTERSECTION_CSV = DATA_OUTPUT_DIR / "intersection_geo.csv"
TRAFFIC_GLOB = str(DEMAND_DIR / "traffic_*.csv")

# Outputs
OSM_OUT = NETWORK_DIR / "Christchurch_Central_City_main_streets.osm.xml"
NET_XML = NETWORK_DIR / "Christchurch_Central_City_main_streets.net.xml"
EDGE_MAP_CSV = NETWORK_DIR / "intersection_to_edges.csv"
OUT_TRIPS = DEMAND_DIR / "traffic_trips.rou.xml"
OUT_ROUTED = DEMAND_DIR / "traffic_trips.routed.rou.xml"
# duarouter default: <routed>.rou.alt.xml (~5x routed size); not used by this project.
OUT_ROUTED_ALT = OUT_ROUTED.with_name(f"{OUT_ROUTED.stem}.alt.xml")
SUMOCFG = ROOT / "Christchurch_Central_City_main_streets.sumocfg"

INTERVAL_SEC = 900
TLS_CYCLE_SEC = 90

# CCC SmartView / Emissions Tracker — cordon loop counters (26 strategic intersections).
# https://ccc.govt.nz/.../traffic-count-data/intersection-traffic-counts-database
CCC_SMARTVIEW_DAILY_CORDON_CARS = 225_000
CCC_SMARTVIEW_MONTHLY_CORDON_TRIPS = 7_040_000  # ~234.7k/day rolling average
CCC_CORDON_DIRECTION_SHARE: dict[str, float] = {
    "south": 0.342,
    "north": 0.236,
    "east": 0.227,
    "west": 0.195,
}

# ECan / Metro — Bus Interchange (Lichfield/Colombo, 16 functional bays).
ECAN_INTERCHANGE_MOVEMENTS_PER_HOUR = 96  # physical ceiling (AVL)
ECAN_INTERCHANGE_DAILY_STOPS_TARGET = 1_250  # mid-point of 1,000–1,500+ daily stops
ECAN_METRO_DAILY_TRIPS_REFERENCE = 4_600  # Greater Christchurch AVL network (context)

# Mid-week working-day hourly shape (index 0 = midnight … 23 = 11 PM).
# Cordon: M-curve — AM peak ~24k/h (7:30–8:30), plateau ~12k/h (10:00–14:00),
# PM peak ~28k/h (16:30–17:30), night <2k/h past midnight (scaled to daily_cars).
CCC_CORDON_HOURLY_VPH: tuple[int, ...] = (
    1_800,
    1_800,
    1_500,
    1_500,
    1_800,
    2_000,  # 00–05
    5_500,
    22_000,
    24_000,
    18_000,  # 06–09
    12_000,
    12_000,
    12_000,
    12_000,
    12_000,  # 10–14
    13_000,
    24_000,
    28_000,
    26_000,
    20_000,  # 15–19
    9_000,
    3_000,
    2_500,
    2_200,  # 20–23
)

# Bus interchange scheduled movements/hour (below 96/h ceiling except peaks).
# AM/PM commuter windows ~75–85/h; midday ~45–55/h; late night ~20–30/h.
ECAN_INTERCHANGE_HOURLY_MOVEMENTS: tuple[int, ...] = (
    25,
    25,
    20,
    20,
    22,
    25,  # 00–05
    45,
    80,
    82,
    55,  # 06–09
    50,
    50,
    48,
    50,
    52,  # 10–14
    55,
    75,
    85,
    82,
    60,  # 15–19
    35,
    28,
    25,
    22,  # 20–23
)
SEARCH_RADIUS_M = 120.0
MAX_CANDIDATES = 8

# OSM node ids: network-boundary stubs promoted to type dead_end (trip from=/to= edges).
BOUNDARY_DEAD_END_JUNCTION_IDS = frozenset(
    {
        "8871638711",
        "5223814601",
        "5223814606",
        "31900631",
        "6112297721",
        "12913290763",
        "6112297866",
        "6112297690",
        "6112297615",
        "6112297380",
        "6112531182",
        "4920064687",
        "12715217264",
        "31813336",
        "7167965156",
        "6104896743",
        "38454865",
        "1359397092",
        "6103866047",
        "4928320693",
        "5321410221",
        "4928708819",
        "6112095263",
        "120192348",
        "883358042",
        "9369372316",  # Moorhouse Ave west clip — keep 1015757969#0 and -1015757969#0
        "6305411590",  # Colombo St south clip (bridge / 31946882)
    }
)
# Selwyn St clip stubs (8871638711): dead_end geometry misaligns with lane shapes and
# buses sink in SUMO when routes use -1015757970#1 / 1015757970#1.
SELWYN_BOUNDARY_JUNCTION_ID = "8871638711"
SELWYN_BOUNDARY_SOURCE_EDGE = "-1015757970#1"
SELWYN_BOUNDARY_SINK_EDGE = "1015757970#1"

# Moorhouse / Colombo TLS cluster — southbound Colombo through 114648686 and 31946882.
MOORHOUSE_COLOMBO_TLS_CLUSTER = (
    "cluster_10970068709_10970068710_10970068711_10970068712_#9more"
)
COLOMBO_SOUTH_FRAGMENT_OSM_WAYS = frozenset(
    {"597576896", "139484443", "114648686", "31946882"}
)
COLOMBO_SOUTH_NET_EDGES = frozenset(
    {
        "597576896#1",
        "597576896#2",
        "139484443",
        "-139484443",
        "114648686#1",
        "-114648686#0",
        "-114648686#1",
        "31946882#1",
        "-31946882#0",
        "-31946882#1",
    }
)
COLOMBO_SOUTH_JUNCTION_IDS = frozenset(
    {
        "7198983662",
        "31898616",
        "31898617",
        "7198983663",
        "10970068712",
        "6305411590",
    }
)
COLOMBO_SOUTH_CLUSTER_NODE = "10970068709"
COLOMBO_SOUTH_INTERNAL_EDGE_PREFIXES = (
    ":7198983662_",
    ":31898616_",
    ":31898617_",
    ":7198983663_",
    ":10970068712_",
    ":6305411590_",
)
COLOMBO_SOUTH_CLUSTER_INTERNAL_EDGE = (
    ":cluster_10970068709_10970068710_10970068711_10970068712_#9more_7"
)
MOORHOUSE_COLOMBO_CLUSTER_INTERNALS = (
    ":cluster_10970068709_10970068710_10970068711_10970068712_#9more_3",
    ":cluster_10970068709_10970068710_10970068711_10970068712_#9more_5",
)

# OSM way ids: drop oneway so netconvert builds reverse edge at clip (e.g. -1015757969#0).
BIDIRECTIONAL_CLIP_OSM_WAY_IDS = frozenset({"1015757969"})

# OSM way ids excluded from filtered OSM / network (duplicate or unwanted street segments).
EXCLUDED_OSM_WAY_IDS = frozenset(
    {
        "192009230",  # Pilgrim Place west extension (use 1018478674)
        # Lichfield / BI redundant service spurs (392044388 connectors remain)
        "1051223207",
        "1051223208",
        "1051223209",
        "539916367",
    }
)

# Overlapping signalised junctions to merge (one controller, no queue between them).
JUNCTION_JOIN_CLUSTERS: tuple[frozenset[str], ...] = (
    frozenset({"31104856", "cluster_1093722421_4948662314"}),
    frozenset({"31064667", "cluster_5317131190_7196771977"}),  # Durham S / Moorhouse
    frozenset({"31814650", "cluster_13518425792_7198983682"}),  # Pilgrim / Manchester
)

# OSM node ids on Oxford Tce / Antigua St mini-roundabout (way junction=roundabout).
ROUNDABOUT_SEED_JUNCTION_IDS = frozenset(
    {
        "7502061549",
        "6028912902",
        "522277772",
    }
)

# Christchurch Bus Interchange — internal bus-only service roads + Lichfield connectors.
BUS_INTERCHANGE_OSM_WAY_IDS = frozenset(
    {
        "369800170",
        "369800173",
        "369800174",
        "392044395",
        "506014262",
        "508372184",
        "508372186",
        "508372189",
        "1329290625",
        "1329290626",
    }
)

# 10 km/h inside Bus Interchange (ECan facility limit).
BUS_INTERCHANGE_SPEED_MS = 2.78
BUS_INTERCHANGE_SPEED_TOL_MS = 0.05

# Junctions at the interchange (Lichfield / Colombo / internal platforms).
BUS_INTERCHANGE_PORTAL_JUNCTION_IDS = frozenset(
    {
        "5312730681",
        "3735573280",
        "3735573281",
        "3735573284",
    }
)

# Core internal loop (bus-only service roads) all buses should traverse.
BUS_INTERCHANGE_INTERNAL_VISIT_EDGES: tuple[str, ...] = (
    "369800174",
    "508372189",
    "508372186",
)

# Official Bus Interchange portals (Lichfield / Colombo service roads).
BUS_INTERCHANGE_ENTRANCE_EDGES: tuple[str, ...] = (
    "369800170#0",
    "-369800173#0",
)
BUS_INTERCHANGE_EXIT_EDGES: tuple[str, ...] = (
    "508372184",
    "369800173#0",
)

# Turn bans inside / at Bus Interchange (from-edge + SUMO dir code).
# Do not ban left from 392044395 — through-route BI exit onto internal Colombo (508372184).
BUS_INTERCHANGE_NO_RIGHT_FROM = frozenset({"508372186", "506014262"})
BUS_INTERCHANGE_NO_LEFT_FROM: frozenset[str] = frozenset()
BUS_INTERCHANGE_NO_UTURN_EDGES = frozenset({"369800173#0", "-369800173#0"})
# Protected portal turns (official Colombo / Lichfield bus exit).
BUS_INTERCHANGE_PROTECTED_CONNECTIONS = frozenset(
    {
        ("392044395", "508372184"),
        (":3735573281_0", "508372184"),
        ("506014262", "508372184"),
        (":3735573281_2", "508372184"),
    }
)

# 30 km/h CBD: public buses use Manchester, Tuam, and St Asaph only (+ interchange links).
CBD_30KPH_SPEED_MS = 8.33
CBD_30KPH_SPEED_TOL_MS = 0.15
# Default motor-lane disallow list (matches netconvert tertiary/secondary types).
STANDARD_MOTOR_LANE_DISALLOW = (
    "tram rail_urban rail rail_electric rail_fast ship container cable_car "
    "subway aircraft wheelchair scooter drone"
)
CBD_BUS_CORRIDOR_STREETS = frozenset(
    {
        "Manchester Street",
        "Tuam Street",
        "Tuam St Bus Lane",
        "Saint Asaph Street",
        "St Asaph Street",
        "Colombo Street",
    }
)
# Lichfield: interchange portals + corridor to Manchester Street (30 km/h bus link).
BUS_INTERCHANGE_LICHFIELD_CONNECTOR_OSM_WAYS = frozenset(
    {
        "392044388",  # Lichfield platform + portal segments
        "777634281",
        "1015728523",  # interchange -> Manchester
        "1228958071",
        "436514739",
    }
)
# Backward edges with OSM bus:lanes designated (restore bus-only lane index 1).
BUS_LICHFIELD_DESIGNATED_BACKWARD_OSM_WAYS = frozenset({"1015728523"})
# OSM busway=opposite_lane with no in-field reverse bus lane (netconvert artifact).
FALSE_OPPOSITE_BUSWAY_BACKWARD_OSM_WAYS = frozenset({"23151049"})  # Tuam Street
# Tuam St Bus Lane edge used when routes must not start on spurious -23151049#*.
TUAM_BUS_LANE_JUNCTION_EDGE = "344479221#4"
TUAM_BUS_LANE_START_EDGE = "344479221#0"
SAINT_ASAPH_WEST_EDGE = "1015728534#0"
SAINT_ASAPH_CONTINUE_EDGE = "1015728533#1"
SAINT_ASAPH_BUS_TURN_EDGE = "27166907#2"
SAINT_ASAPH_COLOMBO_JUNCTION_ID = (
    "cluster_10993270282_10993270283_10993270284_1166224842_#6more"
)
SAINT_ASAPH_ANTIGUA_JUNCTION_ID = "7502061543"
# Edges between Saint Asaph straight and Tuam that bus lane replaces.
SAINT_ASAPH_BUS_LANE_BYPASS_EDGES = frozenset(
    {
        "1015728533#1",
        "1015728532",
        "114915930",
        "337504003#0",
        "777634282#1",
        "-777634282#1",
        "479999311#0",
        "-479999311#0",
    }
)
# Buses must stay on the bus lane through Hagley (no right / U-turn off 344479221#3/#4).
TUAM_BUS_LANE_NO_RIGHT_UTURN_FROM = frozenset({"344479221#3", "344479221#4"})
TUAM_BUS_LANE_STRAIGHT_EXIT = "650742655#2"
TUAM_BUS_LANE_FORBIDDEN_EXITS = frozenset({"41646975", "23151049#1"})
TUAM_BUS_LANE_SEQUENCE = (
    TUAM_BUS_LANE_START_EDGE,
    "344479221#1",
    "344479221#2",
    "344479221#3",
    TUAM_BUS_LANE_JUNCTION_EDGE,
)
# Wrong turns off Saint Asaph (1015728534#0) that skip the Tuam St Bus Lane.
SAINT_ASAPH_WRONG_TURN_EDGES = frozenset(
    {
        "926384058#0",
        "-1015756824#1",
    }
)
# Saint Asaph straight -> Tuam bus lane -> Riccarton or Hagley (27166907#2 = Antigua turn).
SAINT_ASAPH_BUS_LANE_PREFIX = (
    SAINT_ASAPH_WEST_EDGE,
    SAINT_ASAPH_BUS_TURN_EDGE,
    TUAM_BUS_LANE_START_EDGE,
    "344479221#1",
    "344479221#2",
    "344479221#3",
    TUAM_BUS_LANE_JUNCTION_EDGE,
)
SAINT_ASAPH_TO_RICCARTON_TAIL = (TUAM_BUS_LANE_STRAIGHT_EXIT, "817337720")
SAINT_ASAPH_TO_HAGLEY_TAIL = ("-479999311#0", "-1447308649")
TUAM_EAST_EDGE = "984247262#2"
# Buses must not use Antigua (926384058 / 114501117 / -981887619), Saint Asaph
# straight (1015728533#1), or the Hagley link (235249812#0).
BUS_FORBIDDEN_EDGES = frozenset(
    {
        "926384058#0",
        "-926384058#0",
        "1015728533#1",
        "235249812#0",
        "114501117",
        "-981887619#0",
    }
)
RICCARTON_TO_TUAM_CORRIDOR = ("777634282#1", "479999311#0", "23151049#1")
HAGLEY_AVE_EAST_EDGE = "479999311#0"
HAGLEY_AVE_TO_TUAM_EXIT = "23151049#1"
HAGLEY_AVE_TO_TUAM_PAIR = (HAGLEY_AVE_EAST_EDGE, HAGLEY_AVE_TO_TUAM_EXIT)
HAGLEY_AVE_BUS_FORBIDDEN_EXITS = frozenset({"650742655#2", "41646975"})
HAGLEY_AVE_BUS_FORBIDDEN_INTERNAL_LANES = frozenset(
    {
        ":cluster_31064419_6103975091_6103975092_6103975093_#4more_6_0",
        ":cluster_31064419_6103975091_6103975092_6103975093_#4more_7_0",
    }
)
HAGLEY_AVE_TO_TUAM_INTERNAL_LANE = (
    ":cluster_31064419_6103975091_6103975092_6103975093_#4more_8_0"
)
HAGLEY_TO_TUAM_CORRIDOR = ("22779530#0", "23151049#1")
HAGLEY_ROUTE_HINTS = frozenset(
    {
        "-1447308649",
        "-479999311#0",
        "194730197#3",
        "194730197#0",
        "-15800235",
        "1467161237",
        "597569351#0",
        "1015756824#1",
        "1015756825#0",
        "1026983480#1",
        "22779530#0",
    }
)
RICCARTON_ROUTE_HINTS = frozenset(
    {
        "817337720",
        TUAM_BUS_LANE_STRAIGHT_EXIT,
        "777406033",
        "777634273#1",
        "777634272#1",
        "777634282#1",
        "479999311#0",
        "337504003#0",
        "114915930",
        "1015728532",
    }
)
TUAM_STREET_JUNCTION_ID = (
    "cluster_31064419_6103975091_6103975092_6103975093_#4more"
)
TUAM_STREET_EAST_JUNCTION_ID = (
    "cluster_11178603759_11178603760_11178603761_1166224721_#5more"
)
# Public Colombo southbound after left turn from westbound Lichfield at the interchange.
BUS_COLOMBO_LICHFIELD_DEPART_OSM_WAYS = frozenset(
    {
        "114648656",
        "1015728524",
        "1015728525",
    }
)
# Lichfield Street Platform L (westbound, OSM way 392044388 segment #1).
BUS_INTERCHANGE_LICHFIELD_PLATFORM_EDGE = "-392044388#1"
# City departures: westbound Lichfield -> southbound Colombo (TLS), not BI service road.
BUS_LICHFIELD_COLOMBO_LEFT_FROM = "-392044388#1"
BUS_LICHFIELD_COLOMBO_LEFT_TO = "114648656#1"

JUNCTION_JOINS_NOD = NETWORK_DIR / "junction_joins.nod.xml"

NETWORK_STEPS = ("network", "map")
DEMAND_STEPS = ("trips", "duarouter")
ALL_STEPS = NETWORK_STEPS + DEMAND_STEPS


def step_progress(action: str, args) -> PipelineStepProgress:
    """Live stderr progress for pipeline and individual steps (unless --no-progress)."""
    enabled = not getattr(args, "no_progress", False)
    return PipelineStepProgress(action, enabled=enabled)


def trip_routing_horizon_sec(trips_path: Path) -> int:
    """Latest flow end time in trips XML (duarouter progress denominator)."""
    max_end = 0
    for _ev, el in ET.iterparse(trips_path, events=("end",)):
        if el.tag == "flow":
            try:
                max_end = max(max_end, int(float(el.get("end", 0))))
            except (TypeError, ValueError):
                pass
        el.clear()
    return max(max_end, 1)


def resolve_sumo_home(explicit: Path | str | None = None) -> Path | None:
    """Prefer C:\\Sumo, then --sumo-home / SUMO_HOME, when bin/netconvert exists."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if DEFAULT_SUMO_HOME not in candidates:
        candidates.append(DEFAULT_SUMO_HOME)
    env_home = os.environ.get("SUMO_HOME")
    if env_home:
        p = Path(env_home)
        if p not in candidates:
            candidates.append(p)
    for home in candidates:
        if (home / "bin" / "netconvert.exe").is_file():
            return home
    return candidates[0] if candidates and candidates[0].is_dir() else None


def setup_sumolib(*, sumo_home: Path | str | None = None) -> Path | None:
    home = resolve_sumo_home(sumo_home)
    if home is None:
        return None
    os.environ["SUMO_HOME"] = str(home)
    tools = home / "tools"
    if tools.is_dir():
        tools_s = str(tools)
        if tools_s not in sys.path:
            sys.path.insert(0, tools_s)
    return home


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\bst\b", "saint", s)
    s = re.sub(r"\bmt\b", "mount", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def name_matches(osm_label: str, allowed: set[str]) -> bool:
    n = norm(osm_label)
    if not n:
        return False
    for full in allowed:
        nf = norm(full)
        if n == nf or nf in n or n in nf:
            return True
    return False


def sumo_bin(name: str, *, sumo_home: Path | str | None = None) -> str:
    env_key = name.upper()
    exe = os.environ.get(env_key)
    if exe:
        return exe
    home = resolve_sumo_home(sumo_home or os.environ.get("SUMO_HOME"))
    if home:
        candidate = home / "bin" / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    return name


# --- Step 1: network ---------------------------------------------------------

def load_main_streets() -> set[str]:
    names: set[str] = set()
    for line in MAIN_STREETS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            names.add(line)
    return names


def find_matching_ways(osm_path: Path, allowed: set[str]) -> tuple[set[str], set[str]]:
    way_ids: set[str] = set()
    node_ids: set[str] = set()
    for _ev, el in ET.iterparse(osm_path, events=("end",)):
        if el.tag != "way":
            continue
        tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
        if "highway" not in tags:
            el.clear()
            continue
        label = tags.get("name") or tags.get("ref") or ""
        if name_matches(label, allowed):
            way_ids.add(el.get("id"))
            for nd in el.findall("nd"):
                node_ids.add(nd.get("ref"))
        el.clear()
    return way_ids, node_ids


def find_osm_extra_bus_lane_ways(
    osm_path: Path, connected_node_ids: set[str],
) -> tuple[set[str], set[str]]:
    """
    Standalone highway=busway segments that meet the main-street graph.

    Examples: Tuam St Bus Lane (344479221, 650742655), short connectors.
    """
    if not connected_node_ids:
        return set(), set()
    way_ids: set[str] = set()
    node_ids: set[str] = set()
    for _ev, el in ET.iterparse(osm_path, events=("end",)):
        if el.tag != "way":
            continue
        tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
        if tags.get("highway") != "busway":
            el.clear()
            continue
        nds = [nd.get("ref") for nd in el.findall("nd")]
        if not set(nds) & connected_node_ids:
            el.clear()
            continue
        way_ids.add(el.get("id"))
        node_ids.update(nds)
        el.clear()
    return way_ids, node_ids


def find_osm_ways_by_ids(
    osm_path: Path, way_ids: frozenset[str]
) -> tuple[set[str], set[str]]:
    """Collect highway OSM ways (and their nodes) by explicit way id."""
    wanted = set(way_ids)
    found_ways: set[str] = set()
    found_nodes: set[str] = set()
    for _ev, el in ET.iterparse(osm_path, events=("end",)):
        if el.tag != "way":
            continue
        wid = el.get("id")
        if wid not in wanted:
            el.clear()
            continue
        tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
        if "highway" not in tags:
            el.clear()
            continue
        found_ways.add(wid)
        for nd in el.findall("nd"):
            found_nodes.add(nd.get("ref"))
        el.clear()
    return found_ways, found_nodes


def filter_excluded_osm_ways(way_ids: set[str]) -> set[str]:
    """Drop excluded OSM ways from the filtered main-streets set."""
    return way_ids - EXCLUDED_OSM_WAY_IDS


def _osm_way_base_edge_id(edge_id: str) -> str:
    """Strip leading minus and #fragment for OSM-way matching."""
    base = edge_id[1:] if edge_id.startswith("-") else edge_id
    return base.split("#", 1)[0]


def net_edges_from_osm_ways(net, osm_way_ids: frozenset[str]) -> set[str]:
    """SUMO edge ids built from the given OSM way ids."""
    wanted = set(osm_way_ids)
    out: set[str] = set()
    for edge in net.getEdges():
        eid = edge.getID()
        if eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) in wanted:
            out.add(eid)
    return out


def _lane_allows_bus(lane) -> bool:
    if lane.allows("bus"):
        return True
    return "bus" in lane.getPermissions()


def _edge_allows_bus(edge) -> bool:
    return any(_lane_allows_bus(lane) for lane in edge.getLanes())


def _lane_allow_add_bus(allow: str | None) -> str:
    tokens = set((allow or "").split()) or {"bicycle"}
    tokens.add("bus")
    order = ("bus", "bicycle", "passenger", "pedestrian", "delivery", "taxi")
    return " ".join(t for t in order if t in tokens)


def _is_cbd_30kph_lane_speed(speed_str: str | None) -> bool:
    try:
        speed = float(speed_str or 0)
    except (TypeError, ValueError):
        return False
    return abs(speed - CBD_30KPH_SPEED_MS) <= CBD_30KPH_SPEED_TOL_MS


def _cbd_bus_corridor_street(street_name: str | None) -> bool:
    return name_matches(street_name or "", set(CBD_BUS_CORRIDOR_STREETS))


def _cbd_bus_lane_exception(edge_id: str, street_name: str | None) -> bool:
    """Keep bus access on corridors, interchange internals, and Lichfield portals."""
    if _cbd_bus_corridor_street(street_name):
        return True
    base = _osm_way_base_edge_id(edge_id)
    if base in BUS_INTERCHANGE_OSM_WAY_IDS or base in BUS_INTERCHANGE_LICHFIELD_CONNECTOR_OSM_WAYS:
        return True
    if base in BUS_COLOMBO_LICHFIELD_DEPART_OSM_WAYS:
        return True
    return False


def _lane_xml_allows_bus(lane: ET.Element) -> bool:
    allow = (lane.get("allow") or "").split()
    disallow = (lane.get("disallow") or "").split()
    if "bus" in allow:
        return True
    if "bus" in disallow:
        return False
    return not allow


def _lane_restore_from_bicycle_only(
    lane: ET.Element, *, block_bus: bool = False
) -> bool:
    """
    busway:left can import a backward edge as allow=bicycle only; restore cars.
    """
    if lane.get("allow") != "bicycle":
        return False
    lane.attrib.pop("allow", None)
    tokens = STANDARD_MOTOR_LANE_DISALLOW.split()
    if block_bus:
        tokens.append("bus")
    lane.set("disallow", " ".join(tokens))
    return True


def _lane_restore_bus_from_disallow(lane: ET.Element) -> bool:
    """Re-allow buses on a lane (remove bus from disallow)."""
    disallow = (lane.get("disallow") or "").split()
    if "bus" not in disallow:
        return False
    tokens = [t for t in disallow if t != "bus"]
    if tokens:
        lane.set("disallow", " ".join(tokens))
    elif lane.get("disallow") is not None:
        lane.attrib.pop("disallow", None)
    return True


def _lane_set_bus_only(lane: ET.Element) -> bool:
    """Bus-only lane (OSM bus:lanes=designated)."""
    changed = False
    if lane.get("allow") != "bus":
        lane.set("allow", "bus")
        changed = True
    if _lane_restore_bus_from_disallow(lane):
        changed = True
    return changed


def _lane_strip_bus_access(lane: ET.Element) -> bool:
    """Remove bus from a lane (allow= or disallow=). Returns True if changed."""
    allow = lane.get("allow")
    disallow = (lane.get("disallow") or "").split()
    changed = False

    if allow == "bus":
        lane.attrib.pop("allow", None)
        changed = True
    elif allow and "bus" in allow.split():
        tokens = [t for t in allow.split() if t != "bus"]
        if tokens:
            lane.set("allow", " ".join(tokens))
        else:
            lane.attrib.pop("allow", None)
        changed = True

    if "bus" not in disallow:
        disallow.append("bus")
        lane.set("disallow", " ".join(disallow))
        changed = True
    return changed


HIGH_STREET_NAME = "High Street"


def _edge_is_high_street(edge: ET.Element) -> bool:
    return (edge.get("name") or "").strip() == HIGH_STREET_NAME


def patch_high_street_no_bus(net_path: Path) -> tuple[int, int]:
    """Remove bus access from all High Street edges (buses do not use High Street)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    edges_touched = 0
    lanes_patched = 0
    conns_removed = 0

    high_street_ids: set[str] = set()
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        if not _edge_is_high_street(edge):
            continue
        high_street_ids.add(eid)
        edge_changed = False
        for lane in edge.findall("lane"):
            if _lane_xml_allows_bus(lane) and _lane_strip_bus_access(lane):
                lanes_patched += 1
                edge_changed = True
        if edge_changed:
            edges_touched += 1

    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        if fr not in high_street_ids and to not in high_street_ids:
            continue
        if not _connection_xml_allows_bus(root, conn):
            continue
        root.remove(conn)
        conns_removed += 1

    if lanes_patched or conns_removed:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"High Street: removed bus from {lanes_patched} lane(s) on {edges_touched} "
            f"edge(s), removed {conns_removed} bus connection(s) -> {net_path.name}"
        )
        _revalidate_net(net_path, detail="High Street no-bus net revalidate")
    return edges_touched, lanes_patched


def patch_bus_forbidden_cbd_edges(net_path: Path) -> tuple[int, int]:
    """Remove bus access from CBD edges buses must not use (Antigua, Saint Asaph straight, Hagley link)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    edges_touched = 0
    lanes_patched = 0
    conns_removed = 0

    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":") or eid not in BUS_FORBIDDEN_EDGES:
            continue
        edge_changed = False
        for lane in edge.findall("lane"):
            if _lane_xml_allows_bus(lane) and _lane_strip_bus_access(lane):
                lanes_patched += 1
                edge_changed = True
        if edge_changed:
            edges_touched += 1

    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        if fr not in BUS_FORBIDDEN_EDGES and to not in BUS_FORBIDDEN_EDGES:
            continue
        if not _connection_xml_allows_bus(root, conn):
            continue
        root.remove(conn)
        conns_removed += 1

    if lanes_patched or conns_removed:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Bus forbidden CBD edges: removed bus from {lanes_patched} lane(s) on "
            f"{edges_touched} edge(s), removed {conns_removed} bus connection(s) "
            f"-> {net_path.name}"
        )
        _revalidate_net(net_path, detail="bus forbidden CBD edges revalidate")
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
    return edges_touched, lanes_patched


def patch_riccarton_avenue_bus_corridor(net_path: Path) -> int:
    """
    Restore bus on Riccarton Avenue links used by CBD bus routes.

    Required for Saint Asaph -> Riccarton (817337720) and Hagley -> Tuam (22779530#0).
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    touch_edges = frozenset(
        {
            "817337720",
            "22779530#0",
            TUAM_BUS_LANE_STRAIGHT_EXIT,
        }
    )
    patched = 0

    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") == "internal":
            junc = _junction_from_internal_id(eid)
            if junc != TUAM_STREET_JUNCTION_ID:
                continue
            for lane in edge.findall("lane"):
                if _lane_restore_bus_from_disallow(lane):
                    patched += 1
            continue
        if edge.get("function") or eid.startswith(":"):
            continue
        if eid not in touch_edges and (edge.get("name") or "") != "Riccarton Avenue":
            continue
        for lane in edge.findall("lane"):
            if _lane_restore_bus_from_disallow(lane):
                patched += 1

    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
        print(
            f"Riccarton Avenue bus corridor: restored bus on {patched} lane(s) "
            f"-> {net_path.name}"
        )
    return patched


def patch_cbd_30kph_bus_corridors(net_path: Path) -> tuple[int, int]:
    """
    In ~30 km/h areas, buses may use Manchester / Tuam / St Asaph corridors only.

    Also keeps Bus Interchange internal roads and short Lichfield portal links.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    edges_touched = 0
    lanes_patched = 0

    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        lanes = edge.findall("lane")
        if not lanes or not all(_is_cbd_30kph_lane_speed(ln.get("speed")) for ln in lanes):
            continue
        if _cbd_bus_lane_exception(eid, edge.get("name")):
            continue
        edge_changed = False
        for lane in lanes:
            if _lane_xml_allows_bus(lane) and _lane_strip_bus_access(lane):
                lanes_patched += 1
                edge_changed = True
        if edge_changed:
            edges_touched += 1

    if lanes_patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"CBD 30 km/h bus corridors: removed bus from {lanes_patched} lane(s) "
            f"on {edges_touched} edge(s) "
            f"(kept {', '.join(sorted(CBD_BUS_CORRIDOR_STREETS))} + interchange) "
            f"-> {net_path.name}"
        )
    return edges_touched, lanes_patched


def patch_lichfield_bus_manchester_connector(net_path: Path) -> tuple[int, int]:
    """
    Restore bus access on Lichfield between Bus Interchange and Manchester Street.

    Runs after patch_cbd_30kph_bus_corridors so connector edges stay bus-routable.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    edges_touched = 0
    lanes_patched = 0

    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        if (edge.get("name") or "") != "Lichfield Street":
            continue
        if _osm_way_base_edge_id(eid) not in BUS_INTERCHANGE_LICHFIELD_CONNECTOR_OSM_WAYS:
            continue
        lanes = edge.findall("lane")
        if not lanes:
            continue

        designated_back = (
            eid.startswith("-")
            and _osm_way_base_edge_id(eid) in BUS_LICHFIELD_DESIGNATED_BACKWARD_OSM_WAYS
            and len(lanes) >= 2
        )
        edge_changed = False
        for lane in lanes:
            if designated_back and int(lane.get("index", 0)) == len(lanes) - 1:
                if _lane_set_bus_only(lane):
                    lanes_patched += 1
                    edge_changed = True
            elif _lane_restore_bus_from_disallow(lane):
                lanes_patched += 1
                edge_changed = True
        if edge_changed:
            edges_touched += 1

    if lanes_patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Lichfield bus connector: restored bus on {lanes_patched} lane(s) "
            f"on {edges_touched} edge(s) (interchange <-> Manchester) "
            f"-> {net_path.name}"
        )
    return edges_touched, lanes_patched


def patch_colombo_lichfield_bus_depart(net_path: Path) -> tuple[int, int]:
    """
    Restore bus access on public Colombo Street south of the Lichfield / Colombo TLS.

    Needed after the CBD 30 km/h corridor strip so City departures can continue after
    ``-392044388#1`` -> ``114648656#1``.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    edges_touched = 0
    lanes_patched = 0

    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) not in BUS_COLOMBO_LICHFIELD_DEPART_OSM_WAYS:
            continue
        lanes = edge.findall("lane")
        if not lanes:
            continue
        edge_changed = False
        for lane in lanes:
            if _lane_restore_bus_from_disallow(lane):
                lanes_patched += 1
                edge_changed = True
        if edge_changed:
            edges_touched += 1

    colombo_edge_ids: set[str] = set()
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") or eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) in BUS_COLOMBO_LICHFIELD_DEPART_OSM_WAYS:
            colombo_edge_ids.add(eid)

    via_lanes: set[str] = set()
    for conn in root.findall("connection"):
        c_fr = conn.get("from") or ""
        c_to = conn.get("to") or ""
        if c_fr in colombo_edge_ids or c_to in colombo_edge_ids:
            via = conn.get("via") or ""
            if via:
                via_lanes.add(via)

    for lane in root.iter("lane"):
        lid = lane.get("id") or ""
        if lid in via_lanes and _lane_restore_bus_from_disallow(lane):
            lanes_patched += 1

    if lanes_patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Colombo Lichfield depart: restored bus on {lanes_patched} lane(s) "
            f"on {edges_touched} edge(s) -> {net_path.name}"
        )
    return edges_touched, lanes_patched


def patch_colombo_street_bus_corridor(net_path: Path) -> tuple[int, int]:
    """
    Restore bus access on Colombo Street through the CBD clip.

    The 30 km/h CBD rule otherwise strips bus from Colombo; routes then jump to
    Tuam/Manchester or the Bus Interchange internal service road and buses look
    like they vanish from Colombo in the GUI.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    edges_touched = 0
    lanes_patched = 0
    colombo_edge_ids: set[str] = set()

    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        if (edge.get("name") or "") != "Colombo Street":
            continue
        lanes = edge.findall("lane")
        if not lanes:
            continue
        edge_changed = False
        for lane in lanes:
            if _lane_restore_bus_from_disallow(lane):
                lanes_patched += 1
                edge_changed = True
        if edge_changed:
            edges_touched += 1
        colombo_edge_ids.add(eid)

    for conn in root.findall("connection"):
        c_fr = conn.get("from") or ""
        c_to = conn.get("to") or ""
        if c_fr not in colombo_edge_ids and c_to not in colombo_edge_ids:
            continue
        via = conn.get("via") or ""
        if not via:
            continue
        lane_el = root.find(f".//lane[@id='{via}']")
        if lane_el is not None and _lane_restore_bus_from_disallow(lane_el):
            lanes_patched += 1

    if lanes_patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
        print(
            f"Colombo Street bus corridor: restored bus on {lanes_patched} lane(s) "
            f"on {edges_touched} edge(s) -> {net_path.name}"
        )
    return edges_touched, lanes_patched


def patch_lichfield_colombo_left_internal_bus(net_path: Path) -> int:
    """
    Allow buses through the Lichfield / Colombo TLS (westbound Lichfield left turn).

    netconvert marks the whole junction cluster internal lanes with disallow=bus and
    sets some connections to state=O; SUMO then rejects bus routes at runtime.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    tls_cluster = "cluster_31814215_6157770735_9398209301_9398209302_#1more"

    conn_patched = 0
    for conn in root.findall("connection"):
        c_fr = conn.get("from") or ""
        c_to = conn.get("to") or ""
        if c_fr == BUS_LICHFIELD_COLOMBO_LEFT_FROM and c_to == BUS_LICHFIELD_COLOMBO_LEFT_TO:
            if (conn.get("state") or "").upper() == "O":
                conn.set("state", "M")
                conn_patched += 1
        elif c_fr == BUS_LICHFIELD_COLOMBO_LEFT_FROM and (conn.get("state") or "").upper() == "O":
            # Other exits from westbound Lichfield were off; keep left as M only.
            pass

    patched = 0
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if not eid.startswith(f":{tls_cluster}"):
            continue
        for lane in edge.findall("lane"):
            if _lane_restore_bus_from_disallow(lane):
                patched += 1

    if patched or conn_patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
        print(
            f"Lichfield-Colombo left: enabled {conn_patched} connection(s), "
            f"restored bus on {patched} internal TLS lane(s) -> {net_path.name}"
        )
    return patched + conn_patched


def patch_busway_left_bicycle_only_lanes(net_path: Path) -> int:
    """
    Fix backward edges where OSM busway:left became allow=bicycle (cars blocked).

    Keeps cycleways / shared paths unchanged; on 30 km/h CBD streets still blocks buses.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    patched = 0
    skip_name = ("cycleway", "shared path", "footway", "path")

    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        name = (edge.get("name") or "").lower()
        if any(s in name for s in skip_name):
            continue
        lanes = edge.findall("lane")
        block_bus = bool(
            lanes
            and all(_is_cbd_30kph_lane_speed(ln.get("speed")) for ln in lanes)
            and not _cbd_bus_lane_exception(eid, edge.get("name"))
        )
        for lane in lanes:
            if _lane_restore_from_bicycle_only(lane, block_bus=block_bus):
                patched += 1

    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"busway:left fix: restored car access on {patched} bicycle-only lane(s) "
            f"-> {net_path.name}"
        )
    return patched


def _false_opposite_busway_backward_edge(edge_id: str) -> bool:
    """True for spurious reverse edges from OSM busway=opposite_lane on one-way roads."""
    if not edge_id or edge_id.startswith(":"):
        return False
    return (
        edge_id.startswith("-")
        and _osm_way_base_edge_id(edge_id) in FALSE_OPPOSITE_BUSWAY_BACKWARD_OSM_WAYS
    )


def patch_tuam_street_junction_bus_access(net_path: Path) -> int:
    """
    Restore bus turns at the Tuam / Hagley TLS after the CBD 30 km/h corridor strip.

    Without this, buses rely on the spurious reverse edge -23151049#1 because internal
    junction lanes and Hagley Avenue (479999311#0) were left bus-disallowed.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    tuam_touch = frozenset(
        {
            "23151049",
            "344479221",
            "650742655",
            "479999311",
            "41646975",
        }
    )
    patched = 0

    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") == "internal":
            junc = _junction_from_internal_id(eid)
            if junc not in (TUAM_STREET_JUNCTION_ID, TUAM_STREET_EAST_JUNCTION_ID):
                continue
            for lane in edge.findall("lane"):
                if _lane_restore_bus_from_disallow(lane):
                    patched += 1
            continue
        if edge.get("function") or eid.startswith(":"):
            continue
        base = _osm_way_base_edge_id(eid)
        if base not in tuam_touch and (edge.get("name") or "") not in (
            "Tuam Street",
            "Tuam St Bus Lane",
            "Hagley Avenue",
        ):
            continue
        for lane in edge.findall("lane"):
            if _lane_restore_bus_from_disallow(lane):
                patched += 1

    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Tuam junction: restored bus on {patched} lane(s) "
            f"-> {net_path.name}"
        )
    return patched


def patch_saint_asaph_bus_lane_entrance(net_path: Path) -> int:
    """
    Allow buses on Saint Asaph (1015728534#0) to turn onto Tuam St Bus Lane (344479221#0).

    CBD 30 km/h stripping blocks bus on Antigua internal lanes and 27166907#2; restore
    the corridor used for straight-through CBD buses at Colombo / Saint Asaph.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    touch_edges = frozenset(
        {
            SAINT_ASAPH_WEST_EDGE,
            SAINT_ASAPH_BUS_TURN_EDGE,
            "-27166907#2",
            TUAM_BUS_LANE_START_EDGE,
        }
    )
    patched = 0

    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") == "internal":
            junc = _junction_from_internal_id(eid)
            if junc not in (
                SAINT_ASAPH_COLOMBO_JUNCTION_ID,
                SAINT_ASAPH_ANTIGUA_JUNCTION_ID,
            ):
                continue
            for lane in edge.findall("lane"):
                if _lane_restore_bus_from_disallow(lane):
                    patched += 1
            continue
        if edge.get("function") or eid.startswith(":"):
            continue
        if eid not in touch_edges and (edge.get("name") or "") not in (
            "Saint Asaph Street",
            "Antigua Street",
            "Tuam St Bus Lane",
        ):
            continue
        if eid == SAINT_ASAPH_CONTINUE_EDGE:
            continue
        for lane in edge.findall("lane"):
            if _lane_restore_bus_from_disallow(lane):
                patched += 1

    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
        print(
            f"Saint Asaph bus lane entrance: restored bus on {patched} lane(s) "
            f"-> {net_path.name}"
        )
    return patched


def patch_tuam_bus_lane_turn_restrictions(
    net_path: Path, *, revalidate: bool = True
) -> dict[str, int]:
    """
    No right turn or U-turn from Tuam St Bus Lane at 344479221#3 / 344479221#4.

    Buses continue straight on 650742655#2 or turn left on Hagley (-479999311#0);
    Oxford Terrace (41646975) and the Tuam U-turn (23151049#1) are blocked.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    removed = {"right": 0, "uturn": 0}

    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        if fr not in TUAM_BUS_LANE_NO_RIGHT_UTURN_FROM:
            continue
        d = (conn.get("dir") or "").lower()
        if d not in ("r", "t"):
            continue
        root.remove(conn)
        removed["uturn" if d == "t" else "right"] += 1

    total = sum(removed.values())
    if total:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            "Tuam bus lane turn bans: "
            f"no right {removed['right']}, no U-turn {removed['uturn']} "
            f"connection(s) -> {net_path.name}"
        )
        if revalidate:
            _revalidate_net(net_path, detail="Tuam bus lane turn bans revalidate")
            n_bus = patch_tuam_street_junction_bus_access(net_path)
            if n_bus:
                print(
                    f"Tuam junction: restored bus on {n_bus} lane(s) "
                    f"after turn-ban revalidate"
                )
            extra = patch_tuam_bus_lane_turn_restrictions(net_path, revalidate=False)
            for key in removed:
                removed[key] += extra.get(key, 0)
            n_hagley = patch_hagley_avenue_bus_to_tuam_restriction(net_path)
            if n_hagley:
                print(
                    f"Hagley Ave -> Tuam: re-applied bus turn ban on {n_hagley} lane(s) "
                    f"after turn-ban revalidate"
                )
    return removed


def patch_hagley_avenue_bus_to_tuam_restriction(net_path: Path) -> int:
    """
    Buses on Hagley Ave (479999311#0) must continue onto Tuam St (23151049#1).

    Blocks bus on junction internal lanes for left (650742655#2) and straight
    (41646975); cars keep all turns.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    patched = 0

    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        for lane in edge.findall("lane"):
            lid = lane.get("id") or ""
            if lid in HAGLEY_AVE_BUS_FORBIDDEN_INTERNAL_LANES:
                lane_changed = _lane_strip_bus_access(lane)
                # SUMO ignores disallow when allow= is set; allow-only is enough.
                if lane.get("allow"):
                    disallow = (lane.get("disallow") or "").split()
                    if "bus" in disallow:
                        tokens = [t for t in disallow if t != "bus"]
                        if tokens:
                            lane.set("disallow", " ".join(tokens))
                        else:
                            lane.attrib.pop("disallow", None)
                        lane_changed = True
                if lane_changed:
                    patched += 1
            elif lid == HAGLEY_AVE_TO_TUAM_INTERNAL_LANE and _lane_restore_bus_from_disallow(
                lane
            ):
                patched += 1
        if eid == HAGLEY_AVE_EAST_EDGE:
            for lane in edge.findall("lane"):
                if _lane_restore_bus_from_disallow(lane):
                    patched += 1

    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
        print(
            f"Hagley Ave -> Tuam: bus may only turn onto {HAGLEY_AVE_TO_TUAM_EXIT} "
            f"({patched} lane(s)) -> {net_path.name}"
        )
    return patched


def find_false_opposite_busway_backward_edges(net_path: Path) -> list[str]:
    """Spurious reverse edges from OSM busway=opposite_lane (e.g. Tuam -23151049#*)."""
    root = ET.parse(net_path).getroot()
    out: list[str] = []
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if _false_opposite_busway_backward_edge(eid):
            out.append(eid)
    return sorted(out)


def remove_false_opposite_busway_backward_edges(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> int:
    """Remove OSM opposite_lane reverse artifacts via netconvert."""
    edges = find_false_opposite_busway_backward_edges(net_path)
    if not edges:
        return 0
    tmp = net_path.with_suffix(".nooppbus.tmp.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(net_path),
        "--output-file",
        str(tmp),
        "--remove-edges.explicit",
        ",".join(edges),
    ]
    detail = "remove opposite busway reverse edges"
    if prog is not None and prog.enabled:
        run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
    else:
        print("running:", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(tmp, net_path)
    print(
        f"opposite busway: removed {len(edges)} spurious reverse edge(s) "
        f"({', '.join(edges)}) -> {net_path.name}"
    )
    return len(edges)


def patch_internal_bicycle_only_junction_lanes(net_path: Path) -> int:
    """
    Restore motor access on internal junction lanes imported as allow=bicycle only.

    OSM foot/cycle paths at junctions can block all car/bus turns while sumolib still
    lists the connection (via lane is bicycle-only).
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    patched = 0
    for edge in root.findall("edge"):
        if edge.get("function") != "internal":
            continue
        for lane in edge.findall("lane"):
            if _lane_restore_from_bicycle_only(lane, block_bus=False):
                patched += 1
    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"internal junction fix: restored motor access on {patched} "
            f"bicycle-only lane(s) -> {net_path.name}"
        )
    return patched


_NET_LANE_PERM_CACHE: dict[str, tuple[list[str], list[str]]] | None = None


def _net_lane_perm_cache(net_path: Path = NET_XML) -> dict[str, tuple[list[str], list[str]]]:
    global _NET_LANE_PERM_CACHE
    if _NET_LANE_PERM_CACHE is not None:
        return _NET_LANE_PERM_CACHE
    cache: dict[str, tuple[list[str], list[str]]] = {}
    for lane in ET.parse(net_path).getroot().iter("lane"):
        lid = lane.get("id")
        if not lid:
            continue
        allow = [t for t in (lane.get("allow") or "").split() if t]
        disallow = [t for t in (lane.get("disallow") or "").split() if t]
        cache[lid] = (allow, disallow)
    _NET_LANE_PERM_CACHE = cache
    return cache


def _lane_id_perm_allows_vclass(
    lane_id: str,
    vclass: str,
    cache: dict[str, tuple[list[str], list[str]]] | None = None,
) -> bool:
    if cache is None:
        cache = _net_lane_perm_cache()
    allow, disallow = cache.get(lane_id, ([], []))
    if vclass in disallow:
        return False
    if not allow:
        return True
    return vclass in allow


def _sumo_connection_allows_vclass(conn, vclass: str) -> bool:
    """True when from, optional via, and to lanes all allow vclass (SUMO routing rules)."""
    if conn.allows(vclass):
        return True
    cache = _net_lane_perm_cache()
    from_lane = conn.getFromLane()
    if not from_lane.allows(vclass) and not _lane_id_perm_allows_vclass(
        from_lane.getID(), vclass, cache
    ):
        return False
    via_id = conn.getViaLaneID()
    if via_id and not _lane_id_perm_allows_vclass(via_id, vclass, cache):
        return False
    to_lane = conn.getToLane()
    if not to_lane.allows(vclass) and not _lane_id_perm_allows_vclass(
        to_lane.getID(), vclass, cache
    ):
        return False
    return True


def is_drivable_vclass(edge, vclass: str = "bus") -> bool:
    """Drivable edge for a vehicle class (used for bus interchange service roads)."""
    fn = edge.getFunction()
    if fn in ("internal", "crossing", "walkingarea", "connector"):
        return False
    eid = edge.getID()
    if eid.startswith(":"):
        return False
    return any(lane.allows(vclass) for lane in edge.getLanes())


def _sumo_connection_is_uturn(conn) -> bool:
    if (conn.getDirection() or "").lower() != "t":
        return False
    try:
        from_e = conn.getFromLane().getEdge().getID()
        to_e = conn.getToLane().getEdge().getID()
        # Split segments on the same OSM way (e.g. Colombo 597576896#1 -> #2) are through.
        if _osm_way_base_edge_id(from_e) == _osm_way_base_edge_id(to_e):
            return False
    except Exception:
        pass
    return True


def _sumo_reverse_edge_id(a: str, b: str) -> bool:
    if a == b:
        return False
    neg = b[1:] if b.startswith("-") else f"-{b}"
    return a == neg


def _edge_on_osm_ways(edge_id: str, osm_way_ids: frozenset[str]) -> bool:
    return _osm_way_base_edge_id(edge_id) in osm_way_ids


def bus_interchange_uturn_exempt_edge_ids(net) -> set[str]:
    """Edges where a bus U-turn is allowed (Bus Interchange internal service roads)."""
    return bus_interchange_internal_edges(net)


def _outgoing_vclass_edge_ids(
    net, edge_id: str, vclass: str = "bus", *, allow_uturn: bool = True
) -> list[str]:
    """Outgoing edges reachable from a lane that allows vclass (not edge-level only)."""
    try:
        from_edge = net.getEdge(edge_id)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for from_lane in from_edge.getLanes():
        if not from_lane.allows(vclass):
            continue
        for conn in from_lane.getOutgoing():
            if not allow_uturn and _sumo_connection_is_uturn(conn):
                continue
            if not _sumo_connection_allows_vclass(conn, vclass):
                continue
            try:
                to_edge = conn.getToLane().getEdge()
            except Exception:
                continue
            if not is_drivable_vclass(to_edge, vclass):
                continue
            tid = to_edge.getID()
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


def shortest_vclass_edge_path(
    net,
    from_edge: str,
    to_edge: str,
    vclass: str = "bus",
    max_seen: int = 8000,
    max_hops: int = 120,
    *,
    allow_uturn: bool = True,
) -> list[str]:
    """Shortest edge path for vclass (BFS), including both ends."""
    if not from_edge or not to_edge:
        return []
    if from_edge == to_edge:
        return [from_edge]
    from collections import deque

    q: deque[tuple[str, list[str]]] = deque([(from_edge, [from_edge])])
    seen = {from_edge}
    while q:
        eid, path = q.popleft()
        if eid == to_edge:
            return path
        if len(path) >= max_hops:
            continue
        if len(seen) >= max_seen:
            continue
        for nid in _outgoing_vclass_edge_ids(
            net, eid, vclass, allow_uturn=allow_uturn
        ):
            if nid not in seen:
                seen.add(nid)
                q.append((nid, path + [nid]))
    return []


def shortest_vclass_edge_path_avoiding(
    net,
    from_edge: str,
    to_edge: str,
    forbidden: frozenset[str] | set[str],
    vclass: str = "bus",
    max_seen: int = 8000,
    max_hops: int = 120,
    *,
    allow_uturn: bool = True,
) -> list[str]:
    """Shortest vclass path that never uses an edge in ``forbidden``."""
    if not from_edge or not to_edge:
        return []
    if from_edge in forbidden or to_edge in forbidden:
        return []
    if from_edge == to_edge:
        return [from_edge]
    from collections import deque

    q: deque[tuple[str, list[str]]] = deque([(from_edge, [from_edge])])
    seen = {from_edge}
    while q:
        eid, path = q.popleft()
        if eid == to_edge:
            return path
        if len(path) >= max_hops:
            continue
        if len(seen) >= max_seen:
            continue
        for nid in _outgoing_vclass_edge_ids(
            net, eid, vclass, allow_uturn=allow_uturn
        ):
            if nid in forbidden or nid in seen:
                continue
            seen.add(nid)
            q.append((nid, path + [nid]))
    return []


def vclass_allow_uturn(vclass: str) -> bool:
    """Buses use left-turn block detours; cars and other modes may U-turn."""
    return vclass != "bus"


def repair_bus_block_turns(
    net,
    edges: list[str],
    vclass: str = "bus",
) -> list[str]:
    """
    Replace intersection U-turn hops with block paths.

    Buses may not use SUMO U-turn connections; each hop must be reachable
    without ``dir="t"`` (left-turn detour around the block instead).
    """
    if not edges:
        return []
    edges = _strip_false_opposite_busway_backward_hops(net, edges, vclass)
    if not edges:
        return []
    edges = _redirect_saint_asaph_straight_to_bus_lane(net, edges, vclass)
    if not edges:
        return []
    edges = _repair_cbd_bus_corridors(net, edges, vclass)
    if not edges:
        return []
    edges = _repair_tuam_bus_lane_forbidden_exits(net, edges, vclass)
    if not edges:
        return []
    allow_uturn = vclass_allow_uturn(vclass)
    out = [edges[0]]
    for target in edges[1:]:
        fr = out[-1]
        if fr == target:
            continue
        if edge_connected_vclass(net, fr, target, vclass, allow_uturn=allow_uturn):
            out.append(target)
            continue
        if vclass == "bus":
            path = shortest_vclass_edge_path_avoiding(
                net,
                fr,
                target,
                BUS_FORBIDDEN_EDGES,
                vclass,
                allow_uturn=allow_uturn,
            )
        else:
            path = shortest_vclass_edge_path(
                net, fr, target, vclass, allow_uturn=allow_uturn
            )
        if not path or len(path) < 2:
            return []
        out.extend(path[1:])
    if vclass == "bus":
        out = _strip_bus_forbidden_edge_hops(net, out, vclass)
    return out


def bus_interchange_internal_edges(net) -> set[str]:
    return net_edges_from_osm_ways(net, BUS_INTERCHANGE_OSM_WAY_IDS)


def bus_interchange_portal_edges(net) -> set[str]:
    """Drivable edges at interchange junctions (Lichfield access + internal roads)."""
    internal = bus_interchange_internal_edges(net)
    out: set[str] = set(internal)
    for jid in BUS_INTERCHANGE_PORTAL_JUNCTION_IDS:
        try:
            node = net.getNode(jid)
        except Exception:
            continue
        for edge in list(node.getIncoming()) + list(node.getOutgoing()):
            if edge.getFunction():
                continue
            eid = edge.getID()
            if eid.startswith(":"):
                continue
            if _edge_allows_bus(edge):
                out.add(eid)
    return out


def _merge_edge_paths(prefix: list[str], suffix: list[str]) -> list[str]:
    """Join two edge paths at the portal (prefix ends where suffix starts)."""
    if not prefix:
        return list(suffix)
    if not suffix:
        return list(prefix)
    if prefix[-1] == suffix[0]:
        return prefix + suffix[1:]
    return prefix + suffix


def preferred_bus_interchange_portals(net) -> list[str]:
    """Ordered interchange entrance edges (Lichfield / Colombo portals)."""
    ordered: list[str] = []
    for eid in BUS_INTERCHANGE_ENTRANCE_EDGES:
        try:
            edge = net.getEdge(eid)
        except Exception:
            continue
        if _edge_allows_bus(edge) and eid not in ordered:
            ordered.append(eid)
    for osm_wid in (
        "508372184",
        "369800174",
        "369800173",
        "777634281",
        "392044388",
    ):
        if osm_wid == "392044388":
            try:
                plat = BUS_INTERCHANGE_LICHFIELD_PLATFORM_EDGE
                if _edge_allows_bus(net.getEdge(plat)) and plat not in ordered:
                    ordered.append(plat)
            except Exception:
                pass
        for eid in sorted(net_edges_from_osm_ways(net, frozenset({osm_wid}))):
            if _edge_allows_bus(net.getEdge(eid)) and eid not in ordered:
                ordered.append(eid)
    return ordered


def _bus_interchange_portal_route_options(
    net, internal: set[str]
) -> list[list[str]]:
    """Feasible bus paths from each official entrance to each official exit."""
    options: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    osm_internal = BUS_INTERCHANGE_OSM_WAY_IDS
    for entry in BUS_INTERCHANGE_ENTRANCE_EDGES:
        for exit_e in BUS_INTERCHANGE_EXIT_EDGES:
            path = shortest_vclass_edge_path(net, entry, exit_e, "bus", allow_uturn=False)
            if not path or len(path) < 2:
                continue
            if not any(
                e in internal or _osm_way_base_edge_id(e) in osm_internal for e in path
            ):
                continue
            key = tuple(path)
            if key not in seen:
                seen.add(key)
                options.append(list(path))
    if not options:
        return _bus_internal_visit_chain_options(net, internal)
    return options


def pick_bus_interchange_portal(
    net, from_edge: str, to_edge: str, portal_edges: set[str]
) -> str | None:
    """First feasible portal from the preferred list with lowest detour cost."""
    best: str | None = None
    best_cost: float | None = None
    for portal in preferred_bus_interchange_portals(net):
        if portal not in portal_edges:
            continue
        leg_in = shortest_vclass_edge_path(net, from_edge, portal, "bus", allow_uturn=False)
        leg_out = shortest_vclass_edge_path(net, portal, to_edge, "bus", allow_uturn=False)
        if not leg_in or not leg_out:
            continue
        cost = sum(net.getEdge(e).getLength() for e in leg_in)
        cost += sum(net.getEdge(e).getLength() for e in leg_out[1:])
        if best_cost is None or cost < best_cost:
            best = portal
            best_cost = cost
    return best


def _bus_internal_visit_chain(
    net,
    internal: set[str],
    core_edges: tuple[str, ...] = BUS_INTERCHANGE_INTERNAL_VISIT_EDGES,
) -> list[str]:
    """One contiguous pass along the platform service roads (bus-only)."""
    core = [e for e in core_edges if e in internal]
    if len(core) < 2:
        return []
    chain = [core[0]]
    for nxt in core[1:]:
        leg = shortest_vclass_edge_path(net, chain[-1], nxt, "bus", allow_uturn=False)
        if not leg or len(leg) < 2:
            return []
        chain.extend(leg[1:])
    return chain


def _bus_internal_visit_chain_options(net, internal: set[str]) -> list[list[str]]:
    """Forward and reverse platform chains (pick cheaper splice)."""
    options: list[list[str]] = []
    for core in (
        BUS_INTERCHANGE_INTERNAL_VISIT_EDGES,
        tuple(reversed(BUS_INTERCHANGE_INTERNAL_VISIT_EDGES)),
    ):
        visit = _bus_internal_visit_chain(net, internal, core)
        if visit and visit not in options:
            options.append(visit)
    return options


def _strip_interchange_internal_edges(chain: list[str], internal: set[str]) -> list[str]:
    """Drop interchange internal edges so we can insert one clean platform pass."""
    stripped = [eid for eid in chain if eid not in internal]
    return stripped if stripped else list(chain)


def _visit_includes_platform_loop(visit: list[str]) -> bool:
    """True when a portal path traverses the platform service roads inside BI."""
    bases = {_osm_way_base_edge_id(e) for e in visit}
    loop_bases = {_osm_way_base_edge_id(e) for e in BUS_INTERCHANGE_INTERNAL_VISIT_EDGES}
    return len(bases & loop_bases) >= 2


def _splice_bus_internal_visit(
    net,
    chain: list[str],
    internal: set[str],
    *,
    require_platform_loop: bool = True,
) -> list[str]:
    """Insert exactly one pass through the interchange via official entrance/exit portals."""
    visit_options = _bus_interchange_portal_route_options(net, internal)
    if require_platform_loop:
        visit_options = [v for v in visit_options if _visit_includes_platform_loop(v)]
        colombo_exit = [v for v in visit_options if v[-1] == "508372184"]
        if colombo_exit:
            visit_options = colombo_exit
    else:
        colombo_left = [
            v
            for v in visit_options
            if len(v) >= 2
            and v[-2:]
            in (
                [BUS_LICHFIELD_COLOMBO_LEFT_FROM, BUS_LICHFIELD_COLOMBO_LEFT_TO],
                ["392044395", "508372184"],
            )
        ]
        public_colombo = [
            v
            for v in colombo_left
            if v[-2:] == [BUS_LICHFIELD_COLOMBO_LEFT_FROM, BUS_LICHFIELD_COLOMBO_LEFT_TO]
        ]
        with_platform = [
            v
            for v in public_colombo
            if BUS_INTERCHANGE_LICHFIELD_PLATFORM_EDGE in v
        ]
        if with_platform:
            visit_options = with_platform
        elif public_colombo:
            visit_options = public_colombo
        elif colombo_left:
            visit_options = colombo_left
    if not visit_options:
        return chain

    base = _strip_interchange_internal_edges(chain, internal)
    best: list[str] | None = None
    best_cost: float | None = None

    for visit in visit_options:
        entry, exit_e = visit[0], visit[-1]
        for i, eid in enumerate(base):
            leg_in = shortest_vclass_edge_path(net, eid, entry, "bus", allow_uturn=False)
            if not leg_in or leg_in[0] != eid:
                continue
            leg_out = shortest_vclass_edge_path(net, exit_e, base[-1], "bus", allow_uturn=False)
            if not leg_out or leg_out[-1] != base[-1]:
                continue
            merged = _merge_edge_paths(leg_in, visit)
            merged = _merge_edge_paths(merged, leg_out)
            if merged[0] != base[0] or merged[-1] != base[-1]:
                continue
            cost = sum(net.getEdge(e).getLength() for e in merged)
            if best_cost is None or cost < best_cost:
                best = merged
                best_cost = cost

    return best if best else chain


def apply_bus_interchange_to_route(
    net,
    from_edge: str,
    to_edge: str,
    route_edges: list[str],
    *,
    require_platform_loop: bool = True,
    route_cache: dict[tuple[str, str, tuple[str, ...]], list[str]] | None = None,
) -> list[str]:
    """Route buses through Bus Interchange internal roads exactly once (no stops)."""
    if not from_edge or not to_edge or from_edge == to_edge:
        return route_edges

    cache_key = (from_edge, to_edge, tuple(route_edges), require_platform_loop)
    if route_cache is not None and cache_key in route_cache:
        return route_cache[cache_key]

    internal = bus_interchange_internal_edges(net)
    chain = list(route_edges) if route_edges else []
    if not chain:
        chain = shortest_vclass_edge_path(net, from_edge, to_edge, "bus", allow_uturn=False)
    if not chain:
        result = route_edges
        if route_cache is not None:
            route_cache[cache_key] = result
        return result

    chain = _splice_bus_internal_visit(
        net, chain, internal, require_platform_loop=require_platform_loop
    )
    if route_cache is not None:
        route_cache[cache_key] = chain
    return chain


def patch_bus_interchange_bus_access(net_path: Path) -> int:
    """
    OSM access=no + bus=designated leaves only bicycle on service edges; allow buses.
    """
    root = ET.parse(net_path).getroot()
    wanted = BUS_INTERCHANGE_OSM_WAY_IDS
    patched = 0
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") or eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) not in wanted:
            continue
        for lane in edge.findall("lane"):
            new_allow = _lane_allow_add_bus(lane.get("allow"))
            if lane.get("allow") != new_allow:
                lane.set("allow", new_allow)
                patched += 1
    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree = ET.ElementTree(root)
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
    return patched


def _connection_exists(root: ET.Element, fr: str, to: str) -> bool:
    for conn in root.findall("connection"):
        if conn.get("from") == fr and conn.get("to") == to:
            return True
    return False


def _is_protected_bi_connection(fr: str, to: str) -> bool:
    return (fr, to) in BUS_INTERCHANGE_PROTECTED_CONNECTIONS


def _ensure_bi_junction_internal_lane_2(root: ET.Element) -> bool:
    """Restore internal lane for platform-loop right turn onto Colombo (lost after netconvert)."""
    if root.find("edge[@id=':3735573281_2']") is not None:
        return False
    int_edge = ET.SubElement(
        root,
        "edge",
        {
            "id": ":3735573281_2",
            "function": "internal",
        },
    )
    ET.SubElement(
        int_edge,
        "lane",
        {
            "id": ":3735573281_2_0",
            "index": "0",
            "allow": "bus bicycle",
            "speed": "2.78",
            "length": "12.10",
            "shape": "1382.42,675.25 1383.36,672.11 1383.76,669.40 1383.91,666.63 1384.09,663.32",
        },
    )
    for junc in root.findall("junction"):
        if junc.get("id") == "3735573281":
            int_lanes = junc.get("intLanes") or ""
            if ":3735573281_2_0" not in int_lanes:
                junc.set(
                    "intLanes",
                    f"{int_lanes} :3735573281_2_0".strip()
                    if int_lanes
                    else ":3735573281_0_0 :3735573281_1_0 :3735573281_2_0",
                )
            break
    return True


def patch_bus_interchange_portal_connections(net_path: Path) -> int:
    """
    Wire official Bus Interchange entrance/exit edges in the SUMO net.

    - Left: Lichfield portal 392044395 -> Colombo 508372184 (City departures).
    - Right: platform road 506014262 -> Colombo 508372184 (internal loop exit).
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    changed = 0
    if _ensure_bi_junction_internal_lane_2(root):
        changed += 1

    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        if fr == "392044395" and to == "508372184":
            conn.set("fromLane", "0")
            conn.set("toLane", "0")
            conn.set("via", ":3735573281_0_0")
            conn.set("dir", "l")
            conn.set("state", "M")
            changed += 1
        elif fr == ":3735573281_0" and to == "508372184":
            conn.set("fromLane", "0")
            conn.set("toLane", "0")
            conn.set("dir", "l")
            conn.set("state", "M")
            changed += 1
        elif fr == "506014262" and to == "508372184":
            conn.set("fromLane", "0")
            conn.set("toLane", "0")
            conn.set("via", ":3735573281_2_0")
            conn.set("dir", "r")
            conn.set("state", "M")
            changed += 1
        elif fr == ":3735573281_2" and to == "508372184":
            conn.set("fromLane", "0")
            conn.set("toLane", "0")
            conn.set("dir", "r")
            conn.set("state", "M")
            changed += 1

    portal_specs = (
        (
            "392044395",
            "508372184",
            {
                "fromLane": "0",
                "toLane": "0",
                "via": ":3735573281_0_0",
                "dir": "l",
                "state": "M",
            },
        ),
        (
            ":3735573281_0",
            "508372184",
            {"fromLane": "0", "toLane": "0", "dir": "l", "state": "M"},
        ),
        (
            "506014262",
            "508372184",
            {
                "fromLane": "0",
                "toLane": "0",
                "via": ":3735573281_2_0",
                "dir": "r",
                "state": "M",
            },
        ),
        (
            ":3735573281_2",
            "508372184",
            {"fromLane": "0", "toLane": "0", "dir": "r", "state": "M"},
        ),
    )
    added = 0
    for fr, to, attrs in portal_specs:
        if not _connection_exists(root, fr, to):
            ET.SubElement(root, "connection", {"from": fr, "to": to, **attrs})
            added += 1

    if not changed and not added:
        return 0

    tree.write(net_path, encoding="UTF-8", xml_declaration=True)
    print(
        f"bus interchange portals: updated {changed}, added {added} Colombo connection(s) "
        f"-> {net_path.name}"
    )
    _revalidate_net(net_path, detail="bus interchange portal net revalidate")
    return changed + added


def _internal_edge_junction_id(edge_id: str) -> str:
    """Junction id from a SUMO internal edge (e.g. ':3735573281_2' or ':cluster_…_0')."""
    body = edge_id[1:] if edge_id.startswith(":") else edge_id
    return body.rsplit("_", 1)[0]


def _junction_id_in_bus_interchange(junction_id: str) -> bool:
    if junction_id in BUS_INTERCHANGE_PORTAL_JUNCTION_IDS:
        return True
    if junction_id.startswith("cluster_"):
        return any(jid in junction_id for jid in BUS_INTERCHANGE_PORTAL_JUNCTION_IDS)
    return False


def _is_bus_interchange_speed(speed_str: str | None) -> bool:
    try:
        speed = float(speed_str or 0)
    except (TypeError, ValueError):
        return False
    return abs(speed - BUS_INTERCHANGE_SPEED_MS) <= BUS_INTERCHANGE_SPEED_TOL_MS


def patch_bus_interchange_speed_limit(net_path: Path) -> tuple[int, int]:
    """
    Enforce 10 km/h on Bus Interchange internal service roads and bus junction geometry.

    Public OSM service edges are usually already 10 km/h from OSM; portal junction internals
    are often inherited at 30 km/h and are capped here.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    speed_str = f"{BUS_INTERCHANGE_SPEED_MS:.2f}"
    lanes_patched = 0
    conns_patched = 0

    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") == "internal":
            if not _junction_id_in_bus_interchange(_internal_edge_junction_id(eid)):
                continue
            for lane in edge.findall("lane"):
                if not _lane_xml_allows_bus(lane):
                    continue
                if _is_bus_interchange_speed(lane.get("speed")):
                    continue
                lane.set("speed", speed_str)
                lanes_patched += 1
            continue
        if edge.get("function") or eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) not in BUS_INTERCHANGE_OSM_WAY_IDS:
            continue
        for lane in edge.findall("lane"):
            if _is_bus_interchange_speed(lane.get("speed")):
                continue
            lane.set("speed", speed_str)
            lanes_patched += 1

    bi_public: set[str] = set()
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if edge.get("function") or eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) in BUS_INTERCHANGE_OSM_WAY_IDS:
            bi_public.add(eid)

    for conn in root.findall("connection"):
        if not _connection_xml_allows_bus(root, conn):
            continue
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        via = conn.get("via") or ""
        via_edge = via.rsplit("_", 1)[0] if via else ""
        in_bi = (
            fr in bi_public
            or to in bi_public
            or (
                fr.startswith(":")
                and _junction_id_in_bus_interchange(_internal_edge_junction_id(fr))
            )
            or (
                to.startswith(":")
                and _junction_id_in_bus_interchange(_internal_edge_junction_id(to))
            )
            or (
                via_edge.startswith(":")
                and _junction_id_in_bus_interchange(_internal_edge_junction_id(via_edge))
            )
        )
        if not in_bi:
            continue
        sp = conn.get("speed")
        if sp is None:
            continue
        try:
            if float(sp) <= BUS_INTERCHANGE_SPEED_MS + BUS_INTERCHANGE_SPEED_TOL_MS:
                continue
        except (TypeError, ValueError):
            continue
        conn.set("speed", speed_str)
        conns_patched += 1

    if lanes_patched or conns_patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Bus Interchange 10 km/h: set {lanes_patched} lane(s), "
            f"{conns_patched} connection(s) -> {net_path.name}"
        )
        # Do not revalidate here — netconvert would reset internal junction speeds.
    return lanes_patched, conns_patched


def patch_bus_interchange_turn_restrictions(net_path: Path) -> dict[str, int]:
    """Apply Bus Interchange movement bans (right / left / U-turn)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    removed = {"right": 0, "left": 0, "uturn": 0}
    drop_internal: set[str] = set()

    def _mark_internal(via: str) -> None:
        int_edge = _internal_edge_from_lane(via)
        if int_edge:
            drop_internal.add(int_edge)
        fr = via if via.startswith(":") else ""
        if fr:
            drop_internal.add(fr)

    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        d = (conn.get("dir") or "").lower()
        via = conn.get("via") or ""
        drop = False
        kind = ""

        if (
            fr in BUS_INTERCHANGE_NO_RIGHT_FROM
            and d == "r"
            and not _is_protected_bi_connection(fr, to)
        ):
            drop, kind = True, "right"
        elif (
            fr in BUS_INTERCHANGE_NO_LEFT_FROM
            and d == "l"
            and not _is_protected_bi_connection(fr, to)
        ):
            drop, kind = True, "left"
        elif fr in BUS_INTERCHANGE_NO_UTURN_EDGES and d == "t":
            drop, kind = True, "uturn"
        elif (
            fr in BUS_INTERCHANGE_NO_UTURN_EDGES
            and to in BUS_INTERCHANGE_NO_UTURN_EDGES
            and fr != to
        ):
            drop, kind = True, "uturn"

        if drop:
            _mark_internal(via)
            if fr.startswith(":"):
                drop_internal.add(fr)
            root.remove(conn)
            removed[kind] += 1

    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        if _is_protected_bi_connection(fr, to):
            continue
        if _connection_uses_internal(conn, drop_internal):
            via = conn.get("via") or ""
            if fr in BUS_INTERCHANGE_NO_UTURN_EDGES and to in BUS_INTERCHANGE_NO_UTURN_EDGES:
                removed["uturn"] += 1
            elif (conn.get("dir") or "").lower() == "t" and (
                fr in BUS_INTERCHANGE_NO_UTURN_EDGES
                or to in BUS_INTERCHANGE_NO_UTURN_EDGES
            ):
                removed["uturn"] += 1
            elif fr in drop_internal or (via and via in drop_internal):
                if fr in BUS_INTERCHANGE_NO_RIGHT_FROM:
                    removed["right"] += 1
                elif fr in BUS_INTERCHANGE_NO_LEFT_FROM:
                    removed["left"] += 1
                else:
                    removed["uturn"] += 1
            _mark_internal(via)
            if fr.startswith(":"):
                drop_internal.add(fr)
            root.remove(conn)

    total = sum(removed.values())
    if total:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            "bus interchange turn bans: "
            f"no right {removed['right']}, no left {removed['left']}, "
            f"no U-turn {removed['uturn']} connection(s) "
            f"-> {net_path.name}"
        )
        # Raw XML edits can orphan lanes; round-trip fixes junction topology for SUMO.
        _revalidate_net(net_path, detail="bus interchange net revalidate")
        n_bus = patch_bus_interchange_bus_access(net_path)
        if n_bus:
            print(
                f"bus interchange: restored bus access on {n_bus} lane(s) "
                f"after revalidate"
            )
    return removed


def _connection_xml_allows_bus(root: ET.Element, conn: ET.Element) -> bool:
    fr = conn.get("from") or ""
    if not fr or fr.startswith(":"):
        return False
    from_lane = conn.get("fromLane")
    for edge in root.findall("edge"):
        if edge.get("id") != fr:
            continue
        lanes = edge.findall("lane")
        if from_lane is not None:
            try:
                idx = int(from_lane)
            except ValueError:
                idx = None
            if idx is not None and 0 <= idx < len(lanes):
                return _lane_xml_allows_bus(lanes[idx])
        return any(_lane_xml_allows_bus(lane) for lane in lanes)
    return False


def patch_bus_no_uturn_connections(net_path: Path) -> int:
    """
    Remove bus U-turn connections at intersections; buses must turn around the block.

    Keeps U-turn connections only between Bus Interchange internal service roads.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    removed = 0
    for conn in list(root.findall("connection")):
        if (conn.get("dir") or "").lower() != "t":
            continue
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        if fr.startswith(":") or not to or to.startswith(":"):
            continue
        if not _connection_xml_allows_bus(root, conn):
            continue
        if _edge_on_osm_ways(fr, BUS_INTERCHANGE_OSM_WAY_IDS) and _edge_on_osm_ways(
            to, BUS_INTERCHANGE_OSM_WAY_IDS
        ):
            continue
        root.remove(conn)
        removed += 1
    if removed:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"bus no U-turn: removed {removed} intersection U-turn connection(s) "
            f"-> {net_path.name}"
        )
        _revalidate_net(net_path, detail="bus no U-turn net revalidate")
    return removed


def find_excluded_net_edges(net_path: Path) -> list[str]:
    """Network edge ids derived from EXCLUDED_OSM_WAY_IDS."""
    root = ET.parse(net_path).getroot()
    excluded = set(EXCLUDED_OSM_WAY_IDS)
    out: list[str] = []
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        if _osm_way_base_edge_id(eid) in excluded:
            out.append(eid)
    return sorted(out)


def remove_excluded_net_edges(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> int:
    """Remove edges built from EXCLUDED_OSM_WAY_IDS via netconvert."""
    edges = find_excluded_net_edges(net_path)
    if not edges:
        return 0
    tmp = net_path.with_suffix(".noexcluded.tmp.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(net_path),
        "--output-file",
        str(tmp),
        "--remove-edges.explicit",
        ",".join(edges),
    ]
    detail = "remove excluded edges"
    if prog is not None and prog.enabled:
        run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
    else:
        print("running:", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(tmp, net_path)
    print(f"removed {len(edges)} excluded edge(s) -> {net_path.name}")
    return len(edges)


def patch_filtered_osm_drop_ways(osm_path: Path, way_ids: frozenset[str]) -> int:
    """Remove listed ways from filtered OSM on disk."""
    if not way_ids or not osm_path.is_file():
        return 0
    tree = ET.parse(osm_path)
    root = tree.getroot()
    removed = 0
    for way in list(root.findall("way")):
        if way.get("id") in way_ids:
            root.remove(way)
            removed += 1
    if removed:
        tree.write(osm_path, encoding="UTF-8", xml_declaration=True)
    return removed


def find_roundabout_osm_ways(
    osm_path: Path,
    seed_nodes: frozenset[str],
) -> tuple[set[str], set[str]]:
    """OSM ways tagged junction=roundabout that touch any seed node."""
    way_ids: set[str] = set()
    node_ids: set[str] = set()
    if not seed_nodes:
        return way_ids, node_ids
    for _ev, el in ET.iterparse(osm_path, events=("end",)):
        if el.tag != "way":
            continue
        tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
        if tags.get("junction") != "roundabout":
            el.clear()
            continue
        nds = [nd.get("ref") for nd in el.findall("nd")]
        if seed_nodes.intersection(nds):
            way_ids.add(el.get("id"))
            node_ids.update(nds)
        el.clear()
    return way_ids, node_ids


def _roundabout_osm_way_valid(osm_path: Path, way_id: str) -> bool:
    """True when a roundabout way in filtered OSM still has node refs."""
    for _ev, el in ET.iterparse(osm_path, events=("end",)):
        if el.tag == "way" and el.get("id") == way_id:
            refs = [nd.get("ref") for nd in el.findall("nd")]
            el.clear()
            return bool(refs) and all(refs)
        el.clear()
    return False


def patch_osm_remove_oneway(osm_path: Path, way_ids: frozenset[str]) -> int:
    """Remove oneway=yes on listed ways so netconvert imports both directions."""
    if not way_ids or not osm_path.is_file():
        return 0
    tree = ET.parse(osm_path)
    root = tree.getroot()
    patched = 0
    for way in root.findall("way"):
        if way.get("id") not in way_ids:
            continue
        for tag in list(way.findall("tag")):
            if tag.get("k") == "oneway":
                way.remove(tag)
                patched += 1
    if patched:
        tree.write(osm_path, encoding="UTF-8", xml_declaration=True)
    return patched


def enrich_filtered_osm_bidirectional_clip_ways(osm_path: Path) -> int:
    """Ensure clip ways that need a reverse edge are not tagged oneway in filtered OSM."""
    return patch_osm_remove_oneway(osm_path, BIDIRECTIONAL_CLIP_OSM_WAY_IDS)


def enrich_filtered_osm_roundabouts(
    osm_in: Path,
    osm_out: Path,
    seed_nodes: frozenset[str],
) -> int:
    """
    Ensure junction=roundabout ways for seed nodes are in filtered OSM.

    Rebuilds the filtered OSM from main streets + roundabout ways (append breaks
    iterparse serialization of child <nd ref=...> elements).
    """
    rb_ways, rb_nodes = find_roundabout_osm_ways(osm_in, seed_nodes)
    if not rb_ways:
        return 0

    if osm_out.is_file():
        present = {
            el.get("id")
            for _ev, el in ET.iterparse(osm_out, events=("end",))
            if el.tag == "way"
        }
        if rb_ways <= present and all(
            _roundabout_osm_way_valid(osm_out, wid) for wid in rb_ways
        ):
            return 0

    allowed = load_main_streets()
    way_ids, node_ids = find_matching_ways(osm_in, allowed)
    way_ids |= rb_ways
    node_ids |= rb_nodes
    write_filtered_osm(osm_in, osm_out, way_ids, node_ids)
    return len(rb_ways)


def _xml_fragment(el: ET.Element) -> str:
    return ET.tostring(el, encoding="unicode")


def write_filtered_osm(osm_in: Path, osm_out: Path, way_ids: set[str], node_ids: set[str]) -> None:
    osm_out.parent.mkdir(parents=True, exist_ok=True)
    with osm_out.open("w", encoding="utf-8", newline="\n") as fout:
        fout.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fout.write('<osm version="0.6" generator="build_simulation.py">\n')
        for _ev, el in ET.iterparse(osm_in, events=("end",)):
            if el.tag == "bounds":
                fout.write("\t" + _xml_fragment(el) + "\n")
                el.clear()
            elif el.tag == "node" and el.get("id") in node_ids:
                fout.write("\t" + _xml_fragment(el) + "\n")
                el.clear()
            elif el.tag == "way" and el.get("id") in way_ids:
                fout.write("\t" + _xml_fragment(el) + "\n")
                el.clear()
        fout.write("</osm>\n")


def run_netconvert(
    osm_path: Path,
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
    detail: str = "netconvert",
) -> None:
    cmd = [
        sumo_bin("netconvert"),
        "--osm-files",
        str(osm_path),
        "--output-file",
        str(net_path),
        "--proj.utm",
        "true",
        "--geometry.remove",
        "true",
        "--roundabouts.guess",
        "true",
        "--junctions.join",
        "true",
        "--lefthand",
        "true",
        "--output.street-names",
        "true",
        "--osm.lane-access",
        "true",
    ]
    if prog is not None and prog.enabled:
        run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
    else:
        print("running:", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(ROOT))


def write_junction_join_nod(
    path: Path, clusters: tuple[frozenset[str], ...]
) -> None:
    """Plain-node join file for netconvert (--node-files)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<nodes xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '       xsi:noNamespaceSchemaLocation="http://sumo.dlr.de/xsd/nodes_file.xsd">',
    ]
    for cluster in clusters:
        nodes = " ".join(sorted(cluster))
        lines.append(f'    <join nodes="{nodes}"/>')
    lines.append("</nodes>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _shape_centroid(shape: str) -> tuple[float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for pt in (shape or "").split():
        if "," not in pt:
            continue
        try:
            x, y = map(float, pt.split(","))
        except ValueError:
            continue
        if abs(x) < COORD_SANITY_LIMIT and abs(y) < COORD_SANITY_LIMIT:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _refresh_net_conv_boundary(root: ET.Element) -> None:
    xs: list[float] = []
    ys: list[float] = []
    for junc in root.findall("junction"):
        if junc.get("type") == "internal":
            continue
        try:
            x = float(junc.get("x", 0))
            y = float(junc.get("y", 0))
        except (TypeError, ValueError):
            continue
        if abs(x) < COORD_SANITY_LIMIT and abs(y) < COORD_SANITY_LIMIT:
            xs.append(x)
            ys.append(y)
    if not xs:
        return
    loc = root.find("location")
    if loc is None:
        loc = ET.Element("location")
        root.insert(0, loc)
    loc.set(
        "convBoundary",
        f"{min(xs):.2f},{min(ys):.2f},{max(xs):.2f},{max(ys):.2f}",
    )


def repair_corrupt_junction_coords(net_path: Path) -> int:
    """Fix junction x/y spoiled by a bad join (stops GUI flicker warnings)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    fixed = 0
    for junc in root.findall("junction"):
        try:
            x = float(junc.get("x", 0))
            y = float(junc.get("y", 0))
        except (TypeError, ValueError):
            continue
        if abs(x) < COORD_SANITY_LIMIT and abs(y) < COORD_SANITY_LIMIT:
            continue
        cent = _shape_centroid(junc.get("shape", ""))
        if cent is None:
            xs: list[float] = []
            ys: list[float] = []
            for lid in (junc.get("incLanes") or "").split():
                lane = root.find(f".//lane[@id='{lid}']")
                if lane is None:
                    continue
                for pt in (lane.get("shape") or "").split():
                    try:
                        lx, ly = map(float, pt.split(","))
                    except ValueError:
                        continue
                    if abs(lx) < COORD_SANITY_LIMIT and abs(ly) < COORD_SANITY_LIMIT:
                        xs.append(lx)
                        ys.append(ly)
            if xs:
                cent = (sum(xs) / len(xs), sum(ys) / len(ys))
        if cent is None:
            print(
                f"warning: could not repair junction {junc.get('id')}",
                file=sys.stderr,
            )
            continue
        junc.set("x", f"{cent[0]:.2f}")
        junc.set("y", f"{cent[1]:.2f}")
        fixed += 1
    if fixed:
        _refresh_net_conv_boundary(root)
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"repaired {fixed} junction(s) with corrupt coordinates -> {net_path.name}"
        )
    return fixed


def _merge_cluster_sets(clusters: list[frozenset[str]]) -> list[frozenset[str]]:
    """Union overlapping junction-id sets (transitive closure)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for cluster in clusters:
        nodes = sorted(cluster)
        if not nodes:
            continue
        for node in nodes:
            parent.setdefault(node, node)
        for node in nodes[1:]:
            union(nodes[0], node)

    groups: dict[str, set[str]] = {}
    for node in parent:
        root_id = find(node)
        groups.setdefault(root_id, set()).add(node)
    return [frozenset(g) for g in groups.values()]


def _run_junction_join_netconvert(
    net_path: Path,
    clusters: list[frozenset[str]],
    *,
    prog: PipelineStepProgress | None = None,
    detail: str = "junction joins",
) -> None:
    write_junction_join_nod(JUNCTION_JOINS_NOD, tuple(clusters))
    tmp = net_path.with_suffix(".joined.tmp.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(net_path),
        "--node-files",
        str(JUNCTION_JOINS_NOD),
        "--output-file",
        str(tmp),
    ]
    if prog is not None and prog.enabled:
        run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
    else:
        print("running:", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(tmp, net_path)
    repair_corrupt_junction_coords(net_path)


def _pending_junction_join_clusters(
    root: ET.Element, clusters: list[frozenset[str]]
) -> list[frozenset[str]]:
    """Keep only clusters whose member junctions are still separate in the net."""
    pending: list[frozenset[str]] = []
    for cluster in clusters:
        present = [
            jid
            for jid in cluster
            if root.find(f'junction[@id="{jid}"]') is not None
        ]
        if len(present) >= 2:
            pending.append(frozenset(present))
    return pending


def apply_junction_joins(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> int:
    """Merge listed overlapping junction clusters via netconvert join descriptions."""
    root = ET.parse(net_path).getroot()
    clusters = _pending_junction_join_clusters(
        root, _merge_cluster_sets(list(JUNCTION_JOIN_CLUSTERS))
    )
    if not clusters:
        return 0
    _run_junction_join_netconvert(net_path, clusters, prog=prog)
    print(
        f"joined {len(clusters)} junction cluster(s) "
        f"({JUNCTION_JOINS_NOD.name}) -> {net_path.name}"
    )
    return len(clusters)


def rebuild_traffic_lights(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
    detail: str = "TLS rebuild",
) -> None:
    """Drop signals on trivial junctions; rebuild the rest as actuated."""
    tmp = net_path.with_suffix(".tls.tmp.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(net_path),
        "--output-file",
        str(tmp),
        "--tls.rebuild",
        "--tls.discard-simple",
        "--tls.default-type",
        "actuated",
        "--tls.cycle.time",
        str(TLS_CYCLE_SEC),
        "--tls.yellow.time",
        "3",
        "--tls.red.time",
        "2",
    ]
    if prog is not None and prog.enabled:
        run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
    else:
        print("running:", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(tmp, net_path)
    setup_sumolib()
    import sumolib.net as sumo_net  # noqa: E402

    n_tls = len(sumo_net.readNet(str(net_path), withPrograms=True).getTrafficLights())
    print(f"rebuilt traffic lights ({n_tls} signals, actuated) -> {net_path}")


def _tls_controlled_link_count(root: ET.Element, tl_id: str) -> int:
    """
    Number of characters per TLS phase state for this signal.

    SUMO uses max(connection linkIndex)+1, not len(junction/request) — request rows
    follow incoming lanes and can be longer than the signal link count.
    """
    max_idx = -1
    for conn in root.findall("connection"):
        if conn.get("tl") != tl_id:
            continue
        max_idx = max(max_idx, int(conn.get("linkIndex") or 0))
    if max_idx >= 0:
        return max_idx + 1
    junc = root.find(f'junction[@id="{tl_id}"]')
    if junc is not None:
        requests = junc.findall("request")
        if requests:
            return len(requests)
    return 0


def patch_actuated_tls_programs(net_path: Path) -> tuple[int, int]:
    """
    Fix actuated TLS warnings: auto-build detectors and trim phase state strings.

    Phase state length must match the junction request count (one char per link).
    Without detectors, actuated controllers warn; build-all-detectors + jam-threshold
    fixes missing-detector warnings.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()

    n_params = 0
    n_trimmed = 0
    tls_params = (
        ("build-all-detectors", "true"),
        ("jam-threshold", "30"),
        ("detector-gap", "2.0"),
        ("show-detectors", "false"),
    )
    for tl_logic in root.findall("tlLogic"):
        if tl_logic.get("type") != "actuated":
            continue
        tid = tl_logic.get("id") or ""
        existing_keys = {p.get("key") for p in tl_logic.findall("param")}
        for key, val in tls_params:
            if key not in existing_keys:
                ET.SubElement(tl_logic, "param", {"key": key, "value": val})
                n_params += 1
        n_links = _tls_controlled_link_count(root, tid)
        if n_links <= 0:
            continue
        for phase in tl_logic.findall("phase"):
            state = phase.get("state") or ""
            if len(state) != n_links:
                phase.set("state", (state + "r" * n_links)[:n_links])
                n_trimmed += 1

    if n_params or n_trimmed:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"actuated TLS: added {n_params} param(s), "
            f"trimmed {n_trimmed} phase state(s) -> {net_path.name}"
        )
    return n_params, n_trimmed


def finalize_actuated_tls(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> None:
    """Left-turn slip rules and actuated TLS programs."""
    patch_left_turn_links(net_path)
    rebuild_traffic_lights(net_path, prog=prog, detail="TLS rebuild")
    patch_left_turn_links(net_path)
    rebuild_traffic_lights(
        net_path, prog=prog, detail="TLS reconcile (post link patch)"
    )
    patch_actuated_tls_programs(net_path)
    ensure_sumocfg_inputs()


def _set_sumocfg_additional_files(
    tree: ET.ElementTree,
    additional: list[str],
    *,
    route_file: str,
    skip_add: set[str],
) -> None:
    """Replace <additional-files> with a comma-separated list (deduped, ordered)."""
    root = tree.getroot()
    input_el = root.find("input")
    if input_el is None:
        return
    names: list[str] = []
    seen: set[str] = set()
    for el in input_el.findall("additional-files"):
        raw = el.get("value", "")
        for part in raw.replace(";", ",").split(","):
            name = part.strip()
            if name and name not in seen and name not in skip_add:
                seen.add(name)
                names.append(name)
    for el in list(input_el.findall("additional-files")):
        input_el.remove(el)
    for required in additional:
        if required not in seen:
            names.append(required)
    order = additional + [n for n in names if n not in additional]
    route_el = input_el.find("route-files")
    if route_el is not None:
        route_el.set("value", route_file)
    ET.SubElement(input_el, "additional-files", {"value": ",".join(order)})


def ensure_sumocfg_inputs() -> None:
    """Register polygons and routes on the project sumocfg."""
    if not SUMOCFG.is_file():
        return
    poly_name = "data/output/network/Christchurch_Central_City_main_streets.add.xml"
    routed_name = f"data/output/demand/{OUT_ROUTED.name}"
    skip_add = {
        "traffic_lights.add.xml",
        "Christchurch_Central_City_tls_detectors.add.xml",
    }

    tree = ET.parse(SUMOCFG)
    _set_sumocfg_additional_files(
        tree,
        [poly_name],
        route_file=routed_name,
        skip_add=skip_add,
    )
    if hasattr(ET, "indent"):
        ET.indent(tree, space="    ")
    tree.write(SUMOCFG, encoding="UTF-8", xml_declaration=True)
    print(f"updated {SUMOCFG.name} (polygons + routes)")


def _edge_base(edge_id: str) -> str:
    return edge_id[1:] if edge_id.startswith("-") else edge_id


def _edge_is_reverse(from_edge: str, to_edge: str) -> bool:
    if from_edge.startswith("-"):
        return to_edge == from_edge[1:]
    if to_edge.startswith("-"):
        return from_edge == to_edge[1:]
    return False


def _dead_end_junction_ids(root: ET.Element) -> set[str]:
    return {j.get("id") for j in root.findall("junction") if j.get("type") == "dead_end"}


def _clipped_dead_end_junction_ids(root: ET.Element) -> set[str]:
    """Netconvert clip dead_ends only (exclude boundary stubs — keep their through edges)."""
    return _dead_end_junction_ids(root) - BOUNDARY_DEAD_END_JUNCTION_IDS


def _junction_from_internal_id(edge_or_lane_id: str) -> str | None:
    """Junction id from internal edge/lane id ':123_0' or ':123_0_0'."""
    if not edge_or_lane_id.startswith(":"):
        return None
    body = edge_or_lane_id[1:]
    idx = body.find("_")
    return body[:idx] if idx > 0 else None


def _internal_edge_from_lane(lane_id: str) -> str | None:
    """Internal edge id from lane id, e.g. ':31064450_5_0' -> ':31064450_5'."""
    if not lane_id.startswith(":"):
        return None
    body = lane_id[1:]
    if "_" not in body:
        return None
    prefix, lane_idx = body.rsplit("_", 1)
    if not lane_idx.isdigit():
        return None
    return f":{prefix}"


def _link_edge_ids(root: ET.Element) -> set[str]:
    """OSM *_link slip roads in the SUMO network."""
    out: set[str] = set()
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        if (edge.get("type") or "").endswith("_link"):
            out.add(eid)
    return out


def _link_junction_ids(root: ET.Element, link_edges: set[str]) -> set[str]:
    juncs: set[str] = set()
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if eid not in link_edges:
            continue
        fr, to = edge.get("from") or "", edge.get("to") or ""
        if fr:
            juncs.add(fr)
        if to:
            juncs.add(to)
    return juncs


def _connection_uses_internal(
    conn: ET.Element, internal_edges: set[str]
) -> bool:
    fr = conn.get("from") or ""
    via = conn.get("via") or ""
    if fr in internal_edges:
        return True
    int_edge = _internal_edge_from_lane(via)
    return int_edge in internal_edges if int_edge else False


def _demote_tls_connection_state(state: str) -> str:
    """Drop TLS control flags on a connection state string."""
    return "".join(
        "M" if ch == "O" else "m" if ch == "o" else ch for ch in state
    )


def _internals_on_link_path(root: ET.Element, link_edges: set[str]) -> set[str]:
    """Internal edges (:jid_idx) carrying traffic that started on a *_link edge."""
    out: set[str] = set()
    pending: set[str] = set(link_edges)
    seen: set[str] = set()
    while pending:
        fr = pending.pop()
        if fr in seen:
            continue
        seen.add(fr)
        for conn in root.findall("connection"):
            if (conn.get("from") or "") != fr:
                continue
            via = conn.get("via") or ""
            int_edge = _internal_edge_from_lane(via)
            if int_edge and int_edge not in out:
                out.add(int_edge)
                pending.add(int_edge)
            to_edge = conn.get("to") or ""
            if to_edge.startswith(":") and to_edge not in out:
                out.add(to_edge)
                pending.add(to_edge)
    return out


def _link_slip_entry_junctions(root: ET.Element, link_edges: set[str]) -> set[str]:
    """Upstream junction at the start of each OSM *_link slip road."""
    entry: set[str] = set()
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if eid not in link_edges:
            continue
        fj = edge.get("from") or ""
        if fj:
            entry.add(fj)
    return entry


def patch_left_turn_links(net_path: Path) -> tuple[int, int, int]:
    """No right turn and no TLS on OSM *_link slip-road edges (e.g. 180754821)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    link_edges = _link_edge_ids(root)
    if not link_edges:
        return 0, 0, 0

    link_internals = _internals_on_link_path(root, link_edges)
    link_entry_juncs = _link_slip_entry_junctions(root, link_edges)

    drop_internal: set[str] = set()
    removed_turns = 0
    for conn in list(root.findall("connection")):
        fr = conn.get("from") or ""
        if fr not in link_edges:
            continue
        if (conn.get("dir") or "").lower() != "r":
            continue
        via = conn.get("via") or ""
        int_edge = _internal_edge_from_lane(via)
        if int_edge:
            drop_internal.add(int_edge)
        root.remove(conn)
        removed_turns += 1

    for conn in list(root.findall("connection")):
        if _connection_uses_internal(conn, drop_internal):
            root.remove(conn)
            removed_turns += 1

    cleared_tls = 0
    for conn in root.findall("connection"):
        fr = conn.get("from") or ""
        to = conn.get("to") or ""
        on_link = (
            fr in link_edges
            or to in link_edges
            or fr in link_internals
            or to in link_internals
        )
        if not on_link:
            continue
        if conn.get("tl"):
            conn.attrib.pop("tl", None)
            conn.attrib.pop("linkIndex", None)
            cleared_tls += 1
        state = conn.get("state")
        if state and ("O" in state or "o" in state):
            conn.set("state", _demote_tls_connection_state(state))

    demoted_juncs = 0
    for junc in root.findall("junction"):
        jid = junc.get("id") or ""
        if jid not in link_entry_juncs:
            continue
        if junc.get("type") != "traffic_light":
            continue
        junc.set("type", "priority")
        demoted_juncs += 1

    if removed_turns or cleared_tls or demoted_juncs:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"left-turn links: removed {removed_turns} right-turn connection(s), "
            f"cleared TLS on {cleared_tls} connection(s), "
            f"demoted {demoted_juncs} slip-entry junction(s) "
            f"({', '.join(sorted(link_edges)[:4])}"
            f"{', ...' if len(link_edges) > 4 else ''}) "
            f"-> {net_path.name}"
        )
    return removed_turns, cleared_tls, demoted_juncs


def find_dead_end_connector_edges(net_path: Path) -> list[str]:
    """Edges between two clipped dead_end junctions (U-turn pockets along borders)."""
    return _dead_end_connector_edges_from_root(ET.parse(net_path).getroot())


def _dead_end_connector_edges_from_root(root: ET.Element) -> list[str]:
    dead = _clipped_dead_end_junction_ids(root)
    out: list[str] = []
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        fr, to = edge.get("from"), edge.get("to")
        if fr in dead and to in dead:
            out.append(eid)
    return sorted(out)


def _dead_end_shape(junc: ET.Element, root: ET.Element) -> str:
    jx = float(junc.get("x", 0))
    jy = float(junc.get("y", 0))
    inc = (junc.get("incLanes") or "").strip().split()
    if inc:
        lane = root.find(f".//lane[@id='{inc[0]}']")
        if lane is not None:
            pts = (lane.get("shape") or "").split()
            if pts:
                lx, ly = map(float, pts[-1].split(","))
                return f"{lx:.2f},{ly:.2f} {jx:.2f},{jy:.2f}"
    return f"{jx - 0.45:.2f},{jy:.2f} {jx + 0.45:.2f},{jy:.2f}"


# West Riccarton clip: 337504016 (2 lanes) must merge into 235384540 at 2434517342.
RICCARTON_WEST_JUNCTION_ID = "2434517342"
RICCARTON_WEST_APPROACH_EDGE = "337504016"
RICCARTON_WEST_EXIT_EDGE = "235384540"
RICCARTON_WEST_INTERNAL_EDGE = ":2434517342_0"


def _shape_points(shape: str) -> list[tuple[float, float]]:
    return [tuple(map(float, token.split(","))) for token in shape.split()]


def _shape_string(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _shape_point_far(a: tuple[float, float], b: tuple[float, float], tol: float = 0.35) -> bool:
    return abs(a[0] - b[0]) > tol or abs(a[1] - b[1]) > tol


def _extend_lane_shape_endpoints(
    lane: ET.Element,
    *,
    prepend: tuple[float, float] | None = None,
    append: tuple[float, float] | None = None,
) -> bool:
    pts = _shape_points(lane.get("shape") or "")
    if not pts:
        return False
    changed = False
    if prepend and _shape_point_far(pts[0], prepend):
        pts = [prepend, *pts]
        changed = True
    if append and _shape_point_far(pts[-1], append):
        pts = [*pts, append]
        changed = True
    if not changed:
        return False
    lane.set("shape", _shape_string(pts))
    length = 0.0
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        length += (dx * dx + dy * dy) ** 0.5
    lane.set("length", f"{length:.2f}")
    return True


def _offset_shape_points(
    points: list[tuple[float, float]], dx: float, dy: float
) -> list[tuple[float, float]]:
    return [(x + dx, y + dy) for x, y in points]


def _edge_lane_element(root: ET.Element, edge_id: str, index: int) -> ET.Element | None:
    for edge in root.findall("edge"):
        if edge.get("id") != edge_id:
            continue
        for lane in edge.findall("lane"):
            if int(lane.get("index", 0)) == index:
                return lane
    return None


def riccarton_west_lane_patch_needed(root: ET.Element) -> bool:
    """True when westbound Riccarton lane 1 cannot reach the west boundary exit."""
    if root.find(f'junction[@id="{RICCARTON_WEST_JUNCTION_ID}"]') is None:
        return False
    approach = root.find(f'edge[@id="{RICCARTON_WEST_APPROACH_EDGE}"]')
    if approach is None or len(approach.findall("lane")) < 2:
        return False
    connected = {
        conn.get("fromLane")
        for conn in root.findall("connection")
        if conn.get("from") == RICCARTON_WEST_APPROACH_EDGE
        and conn.get("to") == RICCARTON_WEST_EXIT_EDGE
    }
    return "1" not in connected


def patch_riccarton_west_lane_geometry(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """
    Connect westbound Riccarton lane 1 at the west boundary junction.

    netconvert often drops 337504016 lane 1 because 235384540 had only one lane.
    Adds the missing exit/internal lane and revalidates the junction with netconvert.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not riccarton_west_lane_patch_needed(root):
        return False

    approach0 = _edge_lane_element(root, RICCARTON_WEST_APPROACH_EDGE, 0)
    approach1 = _edge_lane_element(root, RICCARTON_WEST_APPROACH_EDGE, 1)
    if approach0 is None or approach1 is None:
        return False

    p0 = _shape_points(approach0.get("shape", ""))
    p1 = _shape_points(approach1.get("shape", ""))
    if not p0 or not p1:
        return False
    dx, dy = p1[-1][0] - p0[-1][0], p1[-1][1] - p0[-1][1]

    exit_edge = root.find(f'edge[@id="{RICCARTON_WEST_EXIT_EDGE}"]')
    if exit_edge is None:
        return False
    if len(exit_edge.findall("lane")) == 1:
        el0 = exit_edge.find("lane")
        if el0 is None:
            return False
        el1 = copy.deepcopy(el0)
        el1.set("id", f"{RICCARTON_WEST_EXIT_EDGE}_1")
        el1.set("index", "1")
        el1.set(
            "shape",
            _shape_string(
                _offset_shape_points(_shape_points(el0.get("shape", "")), dx, dy)
            ),
        )
        exit_edge.append(el1)

    exit_lane1 = _edge_lane_element(root, RICCARTON_WEST_EXIT_EDGE, 1)
    if exit_lane1 is None:
        return False
    exit_start = _shape_points(exit_lane1.get("shape", ""))[0]

    internal = root.find(f'edge[@id="{RICCARTON_WEST_INTERNAL_EDGE}"]')
    if internal is None:
        return False
    int0 = internal.find("lane")
    if int0 is None:
        return False
    via_lane_id = f"{RICCARTON_WEST_INTERNAL_EDGE}_1"
    if internal.find(f'lane[@id="{via_lane_id}"]') is None:
        int_pts = _offset_shape_points(_shape_points(int0.get("shape", "")), dx, dy)
        int_pts[0] = p1[-1]
        int_pts[-1] = exit_start
        int1 = copy.deepcopy(int0)
        int1.set("id", via_lane_id)
        int1.set("index", "1")
        int1.set("shape", _shape_string(int_pts))
        internal.append(int1)

    def add_connection(
        from_e: str, to_e: str, from_lane: str, to_lane: str, via: str = ""
    ) -> None:
        for conn in root.findall("connection"):
            if (
                conn.get("from") == from_e
                and conn.get("to") == to_e
                and conn.get("fromLane") == from_lane
                and conn.get("toLane") == to_lane
            ):
                return
        conn = ET.Element("connection")
        conn.set("from", from_e)
        conn.set("to", to_e)
        conn.set("fromLane", from_lane)
        conn.set("toLane", to_lane)
        conn.set("dir", "s")
        conn.set("state", "M")
        if via:
            conn.set("via", via)
        root.append(conn)

    add_connection(
        RICCARTON_WEST_APPROACH_EDGE,
        RICCARTON_WEST_EXIT_EDGE,
        "1",
        "1",
        via_lane_id,
    )
    add_connection(RICCARTON_WEST_INTERNAL_EDGE, RICCARTON_WEST_EXIT_EDGE, "1", "1")

    prepared = net_path.with_suffix(".riccarton_lane.tmp.xml")
    validated = net_path.with_suffix(".riccarton_lane.out.xml")
    tree.write(prepared, encoding="UTF-8", xml_declaration=True)
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(prepared),
        "--output-file",
        str(validated),
    ]
    detail = "Riccarton west lane fix"
    try:
        if prog is not None and prog.enabled:
            run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
        else:
            print("running:", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=str(ROOT))
        replace_net_file(validated, net_path)
    finally:
        prepared.unlink(missing_ok=True)
        if validated.is_file() and validated != net_path:
            validated.unlink(missing_ok=True)

    print(
        f"patched Riccarton west junction ({RICCARTON_WEST_JUNCTION_ID}): "
        f"connected {RICCARTON_WEST_APPROACH_EDGE} lane 1 -> "
        f"{RICCARTON_WEST_EXIT_EDGE} -> {net_path.name}"
    )
    return True


def _net_insert_before_connections(root: ET.Element, elem: ET.Element) -> None:
    """Insert edge/junction/tlLogic before <connection> so netconvert loads them."""
    for idx, child in enumerate(root):
        if child.tag == "connection":
            root.insert(idx, elem)
            return
    root.append(elem)


def _net_append_connection(root: ET.Element, conn: ET.Element) -> None:
    """Append a connection within the connections section (not after late edges)."""
    insert_at = len(root)
    for idx, child in enumerate(root):
        if child.tag not in ("connection",):
            insert_at = idx
            break
    last_conn = -1
    for idx, child in enumerate(root):
        if child.tag == "connection":
            last_conn = idx
    if last_conn >= 0:
        root.insert(last_conn + 1, conn)
    else:
        root.insert(insert_at, conn)


def _write_colombo_south_fragment_osm(path: Path) -> None:
    """Minimal OSM with Colombo south of Moorhouse (597576896 … 31946882)."""
    root_in = ET.parse(OSM_IN).getroot()
    need_nodes: set[str] = set()
    ways_out: list[ET.Element] = []
    for way in root_in.findall("way"):
        if way.get("id") not in COLOMBO_SOUTH_FRAGMENT_OSM_WAYS:
            continue
        ways_out.append(way)
        for nd in way.findall("nd"):
            need_nodes.add(nd.get("ref"))
    nodes_by_id = {n.get("id"): n for n in root_in.findall("node")}
    planet_path = NETWORK_DIR / "planet_christchurch_central_city.osm"
    if planet_path.is_file():
        for n in ET.parse(planet_path).getroot().findall("node"):
            nid = n.get("id")
            if nid in need_nodes and nid not in nodes_by_id:
                nodes_by_id[nid] = n
    nodes_out = [nodes_by_id[nid] for nid in sorted(need_nodes, key=int) if nid in nodes_by_id]
    bounds = root_in.find("bounds")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<osm version="0.6" generator="sim_pipeline.py">',
    ]
    if bounds is not None:
        lines.append(ET.tostring(bounds, encoding="unicode"))
    for node in nodes_out:
        lines.append(ET.tostring(node, encoding="unicode"))
    for way in ways_out:
        lines.append(ET.tostring(way, encoding="unicode"))
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")


def _netconvert_colombo_south_fragment(frag_net: Path) -> None:
    frag_osm = frag_net.with_suffix(".osm.xml")
    _write_colombo_south_fragment_osm(frag_osm)
    cmd = [
        sumo_bin("netconvert"),
        "--osm-files",
        str(frag_osm),
        "--output-file",
        str(frag_net),
        "--proj.utm",
        "true",
        "--geometry.remove",
        "true",
        "--lefthand",
        "true",
        "--output.street-names",
        "true",
    ]
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def _transform_shape(shape: str, dx: float, dy: float) -> str:
    return _shape_string(_offset_shape_points(_shape_points(shape), dx, dy))


def colombo_south_edges_missing(root: ET.Element) -> bool:
    """True when southbound Colombo edges are absent at the Moorhouse cluster."""
    if root.find(f'junction[@id="{MOORHOUSE_COLOMBO_TLS_CLUSTER}"]') is None:
        return False
    required = ("597576896#1", "597576896#2", "139484443")
    for eid in required:
        edge = root.find(f'edge[@id="{eid}"]')
        if edge is None or edge.get("function"):
            return True
        if eid == "597576896#1" and edge.get("from") != MOORHOUSE_COLOMBO_TLS_CLUSTER:
            return True
    return False


def colombo_south_wiring_needed(root: ET.Element) -> bool:
    """True when southbound Colombo edges exist but lack drive-through connections."""
    if colombo_south_edges_missing(root):
        return False
    for from_e, to_e in (
        ("597576896#0", "597576896#1"),
        ("597576896#1", "597576896#2"),
        ("597576896#2", "139484443"),
        ("139484443", "-114648686#1"),
    ):
        found = False
        for conn in root.findall("connection"):
            if conn.get("from") == from_e and conn.get("to") == to_e:
                found = True
                break
        if not found:
            return True
    return False


def fix_colombo_south_segment_dirs(root: ET.Element) -> bool:
    """
    597576896#1 -> #2 is straight-through Colombo southbound, not a U-turn.

    netconvert / fragment merge sometimes marks the segment joint dir=\"T\" and
    inserts a curved internal lane; both break bus routing and look wrong in GUI.
    """
    if colombo_south_edges_missing(root):
        return False
    changed = False
    for from_e, to_e in (
        ("597576896#1", "597576896#2"),
        (":7198983662_0", "597576896#2"),
    ):
        for conn in root.findall("connection"):
            if conn.get("from") == from_e and conn.get("to") == to_e:
                if (conn.get("dir") or "").upper() == "T":
                    conn.set("dir", "s")
                    changed = True

    int_edge = root.find('edge[@id=":7198983662_0"]')
    if int_edge is not None:
        for lane_idx in range(2):
            from_lane = _edge_lane_element(root, "597576896#1", lane_idx)
            to_lane = _edge_lane_element(root, "597576896#2", lane_idx)
            ilane = int_edge.find(f'lane[@index="{lane_idx}"]')
            if from_lane is None or to_lane is None or ilane is None:
                continue
            from_pts = _shape_points(from_lane.get("shape", ""))
            to_pts = _shape_points(to_lane.get("shape", ""))
            if not from_pts or not to_pts:
                continue
            p0, p1 = from_pts[-1], to_pts[0]
            straight = _shape_string([p0, p1])
            if ilane.get("shape") != straight:
                ilane.set("shape", straight)
                ilane.set("length", f"{((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2) ** 0.5:.2f}")
                changed = True
    return changed


MOORHOUSE_COLOMBO_NORTH_JUNCTION = "3055020544"
COLOMBO_MOORHOUSE_UTURN_FROM = "-597576896#0"
COLOMBO_MOORHOUSE_UTURN_TO = "597576896#0"


def _colombo_moorhouse_uturn_present(root: ET.Element) -> bool:
    for conn in root.findall("connection"):
        if conn.get("from") == "-597576896#0" and conn.get("to") == "597576896#0":
            return True
    return False


def restore_colombo_moorhouse_uturn(root: ET.Element) -> bool:
    """
    Restore north-end Colombo U-turn (-597576896#0 -> 597576896#0).

    Moorhouse approaches can only enter northbound Colombo at the TLS cluster;
    southbound demand uses this turnaround at junction 3055020544. This is
    separate from the 597576896#1 segment joint (straight-through, not U-turn).
    """
    if _colombo_moorhouse_uturn_present(root):
        return False
    from_lane = _edge_lane_element(root, "-597576896#0", 0)
    to_lane = _edge_lane_element(root, "597576896#0", 1)
    to_lane_idx = "1"
    if to_lane is None:
        to_lane = _edge_lane_element(root, "597576896#0", 0)
        to_lane_idx = "0"
    if from_lane is None or to_lane is None:
        return False

    from_pts = _shape_points(from_lane.get("shape", ""))
    to_pts = _shape_points(to_lane.get("shape", ""))
    if not from_pts or not to_pts:
        return False
    p0, p1 = from_pts[-1], to_pts[0]
    mid = (p0[0] + 1.8, p0[1] + 1.4)
    shape = _shape_string([p0, mid, p1])
    length = ((mid[0] - p0[0]) ** 2 + (mid[1] - p0[1]) ** 2) ** 0.5
    length += ((p1[0] - mid[0]) ** 2 + (p1[1] - mid[1]) ** 2) ** 0.5

    int_id = ":3055020544_3"
    int_lane_id = f"{int_id}_0"
    if root.find(f'edge[@id="{int_id}"]') is None:
        int_edge = ET.Element("edge", id=int_id, function="internal")
        lane = ET.Element("lane", id=int_lane_id, index="0")
        lane.set("disallow", from_lane.get("disallow", ""))
        lane.set("speed", from_lane.get("speed", "8.33"))
        lane.set("length", f"{length:.2f}")
        lane.set("shape", shape)
        int_edge.append(lane)
        _net_insert_before_connections(root, int_edge)

    junc = root.find(f'junction[@id="{MOORHOUSE_COLOMBO_NORTH_JUNCTION}"]')
    if junc is not None:
        int_lanes = [x for x in junc.get("intLanes", "").split() if x]
        if int_lane_id not in int_lanes:
            int_lanes.append(int_lane_id)
            junc.set("intLanes", " ".join(int_lanes))

    _add_net_connection(
        root,
        "-597576896#0",
        "597576896#0",
        "0",
        to_lane_idx,
        via=int_lane_id,
        dir_="T",
        state="=",
    )
    _add_net_connection(
        root,
        int_id,
        "597576896#0",
        "0",
        to_lane_idx,
        dir_="T",
        state="M",
    )
    return True


def remove_colombo_moorhouse_uturn(root: ET.Element) -> bool:
    """Drop the artificial U-turn at junction 3055020544 (not allowed on-street)."""
    if not _colombo_moorhouse_uturn_present(root):
        return False
    int_lane = ":3055020544_3_0"
    for conn in list(root.findall("connection")):
        fe = conn.get("from") or ""
        te = conn.get("to") or ""
        via = conn.get("via") or ""
        if fe == COLOMBO_MOORHOUSE_UTURN_FROM and te == COLOMBO_MOORHOUSE_UTURN_TO:
            root.remove(conn)
        elif via == int_lane or fe == ":3055020544_3":
            root.remove(conn)
    for edge in list(root.findall("edge")):
        eid = edge.get("id") or ""
        if eid.startswith(":3055020544_3"):
            root.remove(edge)
    junc = root.find(f'junction[@id="{MOORHOUSE_COLOMBO_NORTH_JUNCTION}"]')
    if junc is not None:
        int_lanes = [
            x
            for x in junc.get("intLanes", "").split()
            if x and not x.startswith(":3055020544_3")
        ]
        junc.set("intLanes", " ".join(int_lanes))
    return True


def apply_colombo_moorhouse_no_uturn(net_path: Path) -> bool:
    """Remove Moorhouse north Colombo U-turn and revalidate the net."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not remove_colombo_moorhouse_uturn(root):
        return False
    prepared = net_path.with_suffix(".moorhouse_no_uturn.tmp.xml")
    tree.write(prepared, encoding="UTF-8", xml_declaration=True)
    validated = net_path.with_suffix(".moorhouse_no_uturn.out.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(prepared),
        "--output-file",
        str(validated),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(validated, net_path)
    prepared.unlink(missing_ok=True)
    return True


MOORHOUSE_COLOMBO_PASSENGER_LANES = (
    ":3055020544_2_0",
    "-807003661_0",
    "807003661_0",
)


def patch_colombo_moorhouse_passenger_through(net_path: Path) -> int:
    """
    Allow cars to continue north on Colombo at Moorhouse (-597576896#0 -> -807003661).

    netconvert tagged the through link as bus/bicycle only; passenger demand must use
  the default straight movement, not a U-turn back south.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    ref = _edge_lane_element(root, "-597576896#0", 0)
    disallow = ref.get("disallow", "") if ref is not None else ""
    patched = 0
    for lane_id in MOORHOUSE_COLOMBO_PASSENGER_LANES:
        lane = root.find(f'.//lane[@id="{lane_id}"]')
        if lane is None:
            continue
        changed_lane = False
        if lane.get("allow") == "bus bicycle":
            del lane.attrib["allow"]
            changed_lane = True
        if disallow and lane.get("disallow") != disallow:
            lane.set("disallow", disallow)
            changed_lane = True
        if changed_lane:
            patched += 1
    if patched:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Colombo Moorhouse: opened passenger through lane(s) on "
            f"{', '.join(MOORHOUSE_COLOMBO_PASSENGER_LANES)} -> {net_path.name}"
        )
    return patched


def apply_colombo_moorhouse_uturn(net_path: Path) -> bool:
    """Persist Moorhouse north Colombo U-turn after south-segment wiring."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not restore_colombo_moorhouse_uturn(root):
        return False
    prepared = net_path.with_suffix(".moorhouse_uturn.tmp.xml")
    tree.write(prepared, encoding="UTF-8", xml_declaration=True)
    validated = net_path.with_suffix(".moorhouse_uturn.out.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(prepared),
        "--output-file",
        str(validated),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(validated, net_path)
    prepared.unlink(missing_ok=True)
    apply_colombo_south_segment_dir_fix(net_path)
    return True


def apply_colombo_south_segment_dir_fix(net_path: Path) -> bool:
    """Persist straight-through Colombo segment joint (no U-turn at 597576896#1)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not fix_colombo_south_segment_dirs(root):
        return False
    tree.write(net_path, encoding="UTF-8", xml_declaration=True)
    return True


def colombo_south_patch_needed(root: ET.Element) -> bool:
    return colombo_south_edges_missing(root) or colombo_south_wiring_needed(root)


def patch_colombo_south_from_moorhouse_cluster(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """
    Import southbound Colombo Street from the Moorhouse TLS cluster.

    netconvert drops 597576896#1/#2 when the cluster is joined; rebuild from an
    isolated OSM fragment and merge geometry into the main network.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not colombo_south_edges_missing(root):
        return False

    anchor_lane = _edge_lane_element(root, "597576896#0", 0)
    if anchor_lane is None:
        anchor_lane = _edge_lane_element(root, "-597576896#0", 0)
    if anchor_lane is None:
        print(
            "warning: Colombo south patch skipped — no anchor lane at Moorhouse cluster",
            file=sys.stderr,
        )
        return False
    anchor_pts = _shape_points(anchor_lane.get("shape", ""))
    if not anchor_pts:
        return False
    anchor_main = anchor_pts[-1]

    frag_net = net_path.with_suffix(".colombo_south.frag.net.xml")
    try:
        _netconvert_colombo_south_fragment(frag_net)
        frag_root = ET.parse(frag_net).getroot()
        frag_lane = frag_root.find('.//lane[@id="597576896#1_0"]')
        if frag_lane is None:
            print("warning: Colombo south fragment missing 597576896#1", file=sys.stderr)
            return False
        frag_pts = _shape_points(frag_lane.get("shape", ""))
        if not frag_pts:
            return False
        dx = anchor_main[0] - frag_pts[0][0]
        dy = anchor_main[1] - frag_pts[0][1]
        node_map = {COLOMBO_SOUTH_CLUSTER_NODE: MOORHOUSE_COLOMBO_TLS_CLUSTER}

        for jid in COLOMBO_SOUTH_JUNCTION_IDS:
            if root.find(f'junction[@id="{jid}"]') is not None:
                continue
            src = frag_root.find(f'junction[@id="{jid}"]')
            if src is None:
                continue
            junc = copy.deepcopy(src)
            junc.set("x", f"{float(junc.get('x', 0)) + dx:.2f}")
            junc.set("y", f"{float(junc.get('y', 0)) + dy:.2f}")
            junc.set("type", "priority")
            junc.set("incLanes", "")
            junc.set("intLanes", "")
            for child in list(junc):
                if child.tag in ("request",):
                    junc.remove(child)
            if "tl" in junc.attrib:
                del junc.attrib["tl"]
            _net_insert_before_connections(root, junc)

        for eid in COLOMBO_SOUTH_NET_EDGES:
            if root.find(f'edge[@id="{eid}"]') is not None:
                continue
            src = frag_root.find(f'edge[@id="{eid}"]')
            if src is None or src.get("function"):
                continue
            edge = copy.deepcopy(src)
            edge.set(
                "from",
                node_map.get(edge.get("from") or "", edge.get("from") or ""),
            )
            edge.set("to", node_map.get(edge.get("to") or "", edge.get("to") or ""))
            shape = edge.get("shape")
            if shape:
                edge.set("shape", _transform_shape(shape, dx, dy))
            for lane in edge.findall("lane"):
                lane.set("id", f"{eid}_{lane.get('index', '0')}")
                lane_shape = lane.get("shape")
                if lane_shape:
                    lane.set("shape", _transform_shape(lane_shape, dx, dy))
            _net_insert_before_connections(root, edge)

        prepared = net_path.with_suffix(".colombo_south.tmp.xml")
        validated = net_path.with_suffix(".colombo_south.out.xml")
        tree.write(prepared, encoding="UTF-8", xml_declaration=True)
        cmd = [
            sumo_bin("netconvert"),
            "--sumo-net-file",
            str(prepared),
            "--output-file",
            str(validated),
        ]
        detail = "Colombo south (Moorhouse cluster)"
        if prog is not None and prog.enabled:
            run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
        else:
            print("running:", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=str(ROOT))
        replace_net_file(validated, net_path)
    finally:
        for p in (
            frag_net,
            frag_net.with_suffix(".osm.xml"),
            net_path.with_suffix(".colombo_south.tmp.xml"),
            net_path.with_suffix(".colombo_south.out.xml"),
        ):
            p.unlink(missing_ok=True)

    print(
        f"imported southbound Colombo from {MOORHOUSE_COLOMBO_TLS_CLUSTER} "
        f"(597576896#1, 597576896#2, 139484443, 114648686) -> {net_path.name}"
    )
    return True


def colombo_south_114648686_missing(root: ET.Element) -> bool:
    """True when core Colombo south exists but OSM way 114648686 is not in the net."""
    if colombo_south_edges_missing(root):
        return False
    return root.find('edge[@id="114648686#0"]') is None


def _import_colombo_south_fragment_edge(
    root: ET.Element,
    frag_root: ET.Element,
    eid: str,
    dx: float,
    dy: float,
    *,
    node_map: dict[str, str],
) -> bool:
    if root.find(f'edge[@id="{eid}"]') is not None:
        return False
    src = frag_root.find(f'edge[@id="{eid}"]')
    if src is None or src.get("function"):
        return False
    edge = copy.deepcopy(src)
    edge.set("from", node_map.get(edge.get("from") or "", edge.get("from") or ""))
    edge.set("to", node_map.get(edge.get("to") or "", edge.get("to") or ""))
    shape = edge.get("shape")
    if shape:
        edge.set("shape", _transform_shape(shape, dx, dy))
    for lane in edge.findall("lane"):
        lane.set("id", f"{eid}_{lane.get('index', '0')}")
        lane_shape = lane.get("shape")
        if lane_shape:
            lane.set("shape", _transform_shape(lane_shape, dx, dy))
    _net_insert_before_connections(root, edge)
    return True


def _sync_colombo_south_junction_from_fragment(
    root: ET.Element,
    frag_root: ET.Element,
    jid: str,
    dx: float,
    dy: float,
) -> bool:
    src = frag_root.find(f'junction[@id="{jid}"]')
    if src is None:
        return False
    dst = root.find(f'junction[@id="{jid}"]')
    if dst is None:
        dst = copy.deepcopy(src)
        _net_insert_before_connections(root, dst)
    else:
        for child in list(dst):
            if child.tag == "request":
                dst.remove(child)
        for attr in ("type", "incLanes", "intLanes"):
            if src.get(attr) is not None:
                dst.set(attr, src.get(attr) or "")
        if "tl" in dst.attrib and src.get("tl") is None:
            del dst.attrib["tl"]
        elif src.get("tl"):
            dst.set("tl", src.get("tl") or "")
    nx = float(src.get("x", 0)) + dx
    ny = float(src.get("y", 0)) + dy
    dst.set("x", f"{nx:.2f}")
    dst.set("y", f"{ny:.2f}")
    src_shape = src.get("shape")
    if src_shape:
        dst.set("shape", _transform_shape(src_shape, dx, dy))
    if dst.get("type") == "dead_end" and jid != "357817713":
        dst.set("type", src.get("type", "priority"))
    return True


def _import_colombo_south_fragment_internals(
    root: ET.Element,
    frag_root: ET.Element,
    dx: float,
    dy: float,
    *,
    prefixes: tuple[str, ...],
) -> int:
    added = 0
    for edge in frag_root.findall("edge"):
        eid = edge.get("id") or ""
        if not any(eid.startswith(prefix) for prefix in prefixes):
            continue
        if root.find(f'edge[@id="{eid}"]') is not None:
            continue
        internal = copy.deepcopy(edge)
        for lane in internal.findall("lane"):
            lane_shape = lane.get("shape")
            if lane_shape:
                lane.set("shape", _transform_shape(lane_shape, dx, dy))
        _net_insert_before_connections(root, internal)
        added += 1
    return added


def _import_colombo_south_114648686_connections(
    root: ET.Element,
    frag_root: ET.Element,
) -> int:
    """Copy fragment connections for 114648686 and junctions 31898617 / 7198983663."""
    colombo_edges = {
        eid
        for eid in COLOMBO_SOUTH_NET_EDGES
        if eid.startswith("114648686") or eid.startswith("-114648686")
    }
    internal_prefixes = (":31898617_", ":7198983663_", ":357817713_")
    allowed_endpoints = colombo_edges | {"139484443", "-139484443"}

    def allowed(endpoint: str) -> bool:
        if not endpoint:
            return True
        if endpoint in allowed_endpoints:
            return True
        return any(endpoint.startswith(prefix) for prefix in internal_prefixes)

    added = 0
    for conn in frag_root.findall("connection"):
        from_e = conn.get("from") or ""
        to_e = conn.get("to") or ""
        via = conn.get("via") or ""
        if not all(allowed(ep) for ep in (from_e, to_e, via)):
            continue
        before = len(root.findall("connection"))
        _add_net_connection(
            root,
            from_e,
            to_e,
            conn.get("fromLane", "0"),
            conn.get("toLane", "0"),
            via=via,
            tl=conn.get("tl", ""),
            link_index=conn.get("linkIndex", ""),
            dir_=conn.get("dir", "s"),
            state=conn.get("state", "M"),
        )
        if len(root.findall("connection")) > before:
            added += 1
    return added


def extend_colombo_south_114648686(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """
    Extend Colombo south with OSM way 114648686 (31898617 -> 357817713).

    Replaces the old 31898617 dead-end clip with a pass-through junction and
    a new boundary stub at 357817713.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not colombo_south_114648686_missing(root):
        return False

    frag_net = net_path.with_suffix(".colombo_south.frag.net.xml")
    added = 0
    try:
        _netconvert_colombo_south_fragment(frag_net)
        frag_root = ET.parse(frag_net).getroot()
        delta = _colombo_south_anchor_delta(root, frag_root)
        if delta is None:
            print(
                "warning: Colombo 114648686 extension skipped — no anchor",
                file=sys.stderr,
            )
            return False
        dx, dy = delta
        node_map = {COLOMBO_SOUTH_CLUSTER_NODE: MOORHOUSE_COLOMBO_TLS_CLUSTER}

        for jid in ("31898617", "7198983663", "357817713"):
            _sync_colombo_south_junction_from_fragment(root, frag_root, jid, dx, dy)

        for eid in COLOMBO_SOUTH_NET_EDGES:
            if not eid.startswith("114648686") and not eid.startswith("-114648686"):
                continue
            _import_colombo_south_fragment_edge(
                root, frag_root, eid, dx, dy, node_map=node_map
            )

        _import_colombo_south_fragment_internals(
            root,
            frag_root,
            dx,
            dy,
            prefixes=(
                ":31898617_",
                ":7198983663_",
                ":357817713_",
            ),
        )

        for tl in frag_root.findall("tlLogic"):
            if tl.get("id") != "7198983663":
                continue
            if root.find('tlLogic[@id="7198983663"]') is None:
                _net_insert_before_connections(root, copy.deepcopy(tl))

        added = _import_colombo_south_114648686_connections(root, frag_root)
        _apply_colombo_south_fragment_geometry(root, frag_root, dx, dy)

        prepared = net_path.with_suffix(".colombo_114648686.tmp.xml")
        validated = net_path.with_suffix(".colombo_114648686.out.xml")
        tree.write(prepared, encoding="UTF-8", xml_declaration=True)
        cmd = [
            sumo_bin("netconvert"),
            "--sumo-net-file",
            str(prepared),
            "--output-file",
            str(validated),
        ]
        detail = "Colombo south 114648686"
        if prog is not None and prog.enabled:
            run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
        else:
            print("running:", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=str(ROOT))
        replace_net_file(validated, net_path)
        patch_boundary_stub_dead_ends(net_path)
        rebuild_colombo_south_geometry_from_osm(net_path, prog=prog)
        apply_colombo_south_segment_dir_fix(net_path)
    finally:
        for p in (
            frag_net,
            frag_net.with_suffix(".osm.xml"),
            net_path.with_suffix(".colombo_114648686.tmp.xml"),
            net_path.with_suffix(".colombo_114648686.out.xml"),
        ):
            p.unlink(missing_ok=True)

    print(
        f"extended Colombo Street South with OSM way 114648686 "
        f"({added} connection(s)) -> {net_path.name}"
    )
    return True


def colombo_south_31946882_missing(root: ET.Element) -> bool:
    """True when 114648686 is present but OSM bridge way 31946882 is not wired."""
    if colombo_south_114648686_missing(root):
        return False
    if root.find('edge[@id="31946882#1"]') is None:
        return True
    clip = root.find('edge[@id="-114648686#0"]')
    return clip is not None and clip.get("to") == "357817713"


def _strip_colombo_clip_at_357817713(root: ET.Element) -> None:
    """Remove the old 357817713 dead-end clip before importing 31946882."""
    for jid in ("357817713",):
        junc = root.find(f'junction[@id="{jid}"]')
        if junc is not None:
            root.remove(junc)
    remove_edges: list[ET.Element] = []
    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if eid == "114648686#0" or eid.startswith(":357817713_"):
            remove_edges.append(edge)
    for edge in remove_edges:
        root.remove(edge)
    for conn in list(root.findall("connection")):
        parts = (conn.get("from") or "", conn.get("to") or "", conn.get("via") or "")
        if any(
            p == "114648686#0"
            or p == "357817713"
            or p.startswith(":357817713_")
            for p in parts
            if p
        ):
            root.remove(conn)


def _replace_colombo_south_fragment_edge(
    root: ET.Element,
    frag_root: ET.Element,
    eid: str,
    dx: float,
    dy: float,
    *,
    node_map: dict[str, str],
) -> bool:
    src = frag_root.find(f'edge[@id="{eid}"]')
    if src is None or src.get("function"):
        return False
    existing = root.find(f'edge[@id="{eid}"]')
    if existing is not None:
        root.remove(existing)
    edge = copy.deepcopy(src)
    edge.set("from", node_map.get(edge.get("from") or "", edge.get("from") or ""))
    edge.set("to", node_map.get(edge.get("to") or "", edge.get("to") or ""))
    shape = edge.get("shape")
    if shape:
        edge.set("shape", _transform_shape(shape, dx, dy))
    for lane in edge.findall("lane"):
        lane.set("id", f"{eid}_{lane.get('index', '0')}")
        lane_shape = lane.get("shape")
        if lane_shape:
            lane.set("shape", _transform_shape(lane_shape, dx, dy))
    _net_insert_before_connections(root, edge)
    return True


def _import_colombo_south_bridge_connections(
    root: ET.Element,
    frag_root: ET.Element,
) -> int:
    """Copy fragment connections for 114648686 / 31946882 south of 7198983663."""
    edge_prefixes = (
        "114648686",
        "-114648686",
        "31946882",
        "-31946882",
        "139484443",
        "-139484443",
    )
    internal_prefixes = (
        ":31898617_",
        ":7198983663_",
        ":10970068712_",
        ":6305411590_",
    )

    def allowed(endpoint: str) -> bool:
        if not endpoint:
            return True
        if any(endpoint.startswith(p) for p in edge_prefixes):
            return True
        return any(endpoint.startswith(p) for p in internal_prefixes)

    added = 0
    for conn in frag_root.findall("connection"):
        from_e = conn.get("from") or ""
        to_e = conn.get("to") or ""
        via = conn.get("via") or ""
        if not all(allowed(ep) for ep in (from_e, to_e, via)):
            continue
        before = len(root.findall("connection"))
        _add_net_connection(
            root,
            from_e,
            to_e,
            conn.get("fromLane", "0"),
            conn.get("toLane", "0"),
            via=via,
            tl=conn.get("tl", ""),
            link_index=conn.get("linkIndex", ""),
            dir_=conn.get("dir", "s"),
            state=conn.get("state", "M"),
        )
        if len(root.findall("connection")) > before:
            added += 1
    return added


def extend_colombo_south_31946882(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """
    Extend Colombo south with OSM bridge way 31946882 (10970068712 -> 6305411590).

    Replaces the 357817713 clip: -114648686#0 continues to TLS 10970068712.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    if not colombo_south_31946882_missing(root):
        return False

    frag_net = net_path.with_suffix(".colombo_south.frag.net.xml")
    added = 0
    try:
        _netconvert_colombo_south_fragment(frag_net)
        frag_root = ET.parse(frag_net).getroot()
        delta = _colombo_south_anchor_delta(root, frag_root)
        if delta is None:
            print(
                "warning: Colombo 31946882 extension skipped — no anchor",
                file=sys.stderr,
            )
            return False
        dx, dy = delta
        node_map = {COLOMBO_SOUTH_CLUSTER_NODE: MOORHOUSE_COLOMBO_TLS_CLUSTER}

        _strip_colombo_clip_at_357817713(root)

        for jid in ("31898617", "7198983663", "10970068712", "6305411590"):
            _sync_colombo_south_junction_from_fragment(root, frag_root, jid, dx, dy)

        replace_edges = (
            "-114648686#0",
            "-114648686#1",
            "114648686#1",
            "31946882#1",
            "-31946882#0",
            "-31946882#1",
        )
        for eid in replace_edges:
            _replace_colombo_south_fragment_edge(
                root, frag_root, eid, dx, dy, node_map=node_map
            )

        _import_colombo_south_fragment_internals(
            root,
            frag_root,
            dx,
            dy,
            prefixes=(
                ":31898617_",
                ":7198983663_",
                ":10970068712_",
                ":6305411590_",
            ),
        )

        for tl_id in ("7198983663", "10970068712"):
            existing = root.find(f'tlLogic[@id="{tl_id}"]')
            if existing is not None:
                root.remove(existing)
            src_tl = frag_root.find(f'tlLogic[@id="{tl_id}"]')
            if src_tl is not None:
                _net_insert_before_connections(root, copy.deepcopy(src_tl))

        added = _import_colombo_south_bridge_connections(root, frag_root)
        _apply_colombo_south_fragment_geometry(root, frag_root, dx, dy)

        prepared = net_path.with_suffix(".colombo_31946882.tmp.xml")
        validated = net_path.with_suffix(".colombo_31946882.out.xml")
        tree.write(prepared, encoding="UTF-8", xml_declaration=True)
        cmd = [
            sumo_bin("netconvert"),
            "--sumo-net-file",
            str(prepared),
            "--output-file",
            str(validated),
        ]
        detail = "Colombo south 31946882"
        if prog is not None and prog.enabled:
            run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
        else:
            print("running:", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=str(ROOT))
        replace_net_file(validated, net_path)
        patch_boundary_stub_dead_ends(net_path)
        rebuild_colombo_south_geometry_from_osm(net_path, prog=prog)
        apply_colombo_south_segment_dir_fix(net_path)
    finally:
        for p in (
            frag_net,
            frag_net.with_suffix(".osm.xml"),
            net_path.with_suffix(".colombo_31946882.tmp.xml"),
            net_path.with_suffix(".colombo_31946882.out.xml"),
        ):
            p.unlink(missing_ok=True)

    print(
        f"extended Colombo Street South with OSM way 31946882 "
        f"({added} connection(s)) -> {net_path.name}"
    )
    return True


def _colombo_south_anchor_delta(
    root: ET.Element, frag_root: ET.Element
) -> tuple[float, float] | None:
    anchor_lane = _edge_lane_element(root, "597576896#0", 0)
    frag_lane = frag_root.find('.//lane[@id="597576896#1_0"]')
    if anchor_lane is None or frag_lane is None:
        return None
    anchor_pts = _shape_points(anchor_lane.get("shape", ""))
    frag_pts = _shape_points(frag_lane.get("shape", ""))
    if not anchor_pts or not frag_pts:
        return None
    return anchor_pts[-1][0] - frag_pts[0][0], anchor_pts[-1][1] - frag_pts[0][1]


def _snap_lane_endpoints(from_lane: ET.Element, to_lane: ET.Element) -> None:
    from_pts = _shape_points(from_lane.get("shape", ""))
    to_pts = _shape_points(to_lane.get("shape", ""))
    if len(from_pts) < 2 or len(to_pts) < 2:
        return
    join = (
        (from_pts[-1][0] + to_pts[0][0]) / 2.0,
        (from_pts[-1][1] + to_pts[0][1]) / 2.0,
    )
    from_pts[-1] = join
    to_pts[0] = join
    from_lane.set("shape", _shape_string(from_pts))
    to_lane.set("shape", _shape_string(to_pts))


def _lane_polyline_length(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for idx in range(1, len(points)):
        dx = points[idx][0] - points[idx - 1][0]
        dy = points[idx][1] - points[idx - 1][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total


def _set_lane_shape(lane: ET.Element, points: list[tuple[float, float]]) -> bool:
    new_shape = _shape_string(points)
    if lane.get("shape") == new_shape:
        return False
    lane.set("shape", new_shape)
    lane.set("length", f"{_lane_polyline_length(points):.2f}")
    return True


def _sync_edge_shape_from_lanes(root: ET.Element, edge_id: str) -> bool:
    edge = root.find(f'edge[@id="{edge_id}"]')
    lane0 = _edge_lane_element(root, edge_id, 0)
    if edge is None or lane0 is None:
        return False
    lane_pts = _shape_points(lane0.get("shape", ""))
    if len(lane_pts) < 2:
        return False
    shape = _shape_string([lane_pts[0], lane_pts[-1]])
    if edge.get("shape") == shape:
        return False
    edge.set("shape", shape)
    return True


def _fix_cluster_colombo_internal_lanes(root: ET.Element) -> bool:
    """Straighten Moorhouse-cluster internals that exit onto Colombo south."""
    changed = False
    for lane_idx, internal_id in enumerate(MOORHOUSE_COLOMBO_CLUSTER_INTERNALS):
        int_edge = root.find(f'edge[@id="{internal_id}"]')
        from_lane = _edge_lane_element(root, "597576896#0", lane_idx)
        to_lane = _edge_lane_element(root, "597576896#1", lane_idx)
        if int_edge is None or from_lane is None or to_lane is None:
            continue
        ilane = int_edge.find('lane[@index="0"]')
        if ilane is None:
            continue
        from_pts = _shape_points(from_lane.get("shape", ""))
        to_pts = _shape_points(to_lane.get("shape", ""))
        if not from_pts or not to_pts:
            continue
        if _set_lane_shape(ilane, [from_pts[-1], to_pts[0]]):
            changed = True
    return changed


def _apply_colombo_south_fragment_geometry(
    root: ET.Element,
    frag_root: ET.Element,
    dx: float,
    dy: float,
) -> bool:
    """
    Copy OSM-fragment lane/junction geometry into the main net.

    OSM way 597576896 runs south through 10970068709 -> 7198983662 -> 31898616;
    way 139484443 continues to 31898617; way 114648686 continues to 357817713.
    """
    if colombo_south_edges_missing(root):
        return False
    changed = False

    for jid in COLOMBO_SOUTH_JUNCTION_IDS:
        src = frag_root.find(f'junction[@id="{jid}"]')
        dst = root.find(f'junction[@id="{jid}"]')
        if src is None or dst is None:
            continue
        nx = float(src.get("x", 0)) + dx
        ny = float(src.get("y", 0)) + dy
        new_x, new_y = f"{nx:.2f}", f"{ny:.2f}"
        if dst.get("x") != new_x or dst.get("y") != new_y:
            dst.set("x", new_x)
            dst.set("y", new_y)
            changed = True
        src_shape = src.get("shape")
        if src_shape:
            new_shape = _transform_shape(src_shape, dx, dy)
            if dst.get("shape") != new_shape:
                dst.set("shape", new_shape)
                changed = True

    for eid in COLOMBO_SOUTH_NET_EDGES:
        src = frag_root.find(f'edge[@id="{eid}"]')
        if src is None or src.get("function"):
            continue
        if root.find(f'edge[@id="{eid}"]') is None:
            continue
        for lane_idx, src_lane in enumerate(src.findall("lane")):
            dst_lane = _edge_lane_element(root, eid, lane_idx)
            if dst_lane is None:
                continue
            pts = _offset_shape_points(
                _shape_points(src_lane.get("shape", "")), dx, dy
            )
            if _set_lane_shape(dst_lane, pts):
                changed = True
        if _sync_edge_shape_from_lanes(root, eid):
            changed = True

    if _fix_cluster_colombo_internal_lanes(root):
        changed = True
    return changed


def rebuild_colombo_south_geometry_from_osm(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """
    Rebuild Colombo south from planet OSM ways 597576896 + 139484443.

    Anchors the isolated netconvert fragment at 597576896#0 (Moorhouse cluster exit).
    """
    if colombo_south_edges_missing(ET.parse(net_path).getroot()):
        return False

    frag_net = net_path.with_suffix(".colombo_south.frag.net.xml")
    try:
        _netconvert_colombo_south_fragment(frag_net)
        tree = ET.parse(net_path)
        root = tree.getroot()
        frag_root = ET.parse(frag_net).getroot()
        delta = _colombo_south_anchor_delta(root, frag_root)
        if delta is None:
            print(
                "warning: Colombo south OSM geometry skipped — no anchor",
                file=sys.stderr,
            )
            return False
        if not _apply_colombo_south_fragment_geometry(root, frag_root, *delta):
            return False

        if fix_colombo_south_segment_dirs(root):
            pass
        prepared = net_path.with_suffix(".colombo_geom.tmp.xml")
        tree.write(prepared, encoding="UTF-8", xml_declaration=True)
        replace_net_file(prepared, net_path)
        apply_colombo_south_segment_dir_fix(net_path)
    finally:
        for p in (
            frag_net,
            frag_net.with_suffix(".osm.xml"),
            net_path.with_suffix(".colombo_geom.tmp.xml"),
        ):
            p.unlink(missing_ok=True)

    print(f"rebuilt Colombo Street South from OSM -> {net_path.name}")
    return True


def _add_net_connection(
    root: ET.Element,
    from_e: str,
    to_e: str,
    from_lane: str,
    to_lane: str,
    *,
    via: str = "",
    tl: str = "",
    link_index: str = "",
    dir_: str = "s",
    state: str = "M",
) -> None:
    for conn in root.findall("connection"):
        if (
            conn.get("from") == from_e
            and conn.get("to") == to_e
            and conn.get("fromLane") == from_lane
            and conn.get("toLane") == to_lane
        ):
            return
    conn = ET.Element("connection")
    conn.set("from", from_e)
    conn.set("to", to_e)
    conn.set("fromLane", from_lane)
    conn.set("toLane", to_lane)
    conn.set("dir", dir_)
    conn.set("state", state)
    if via:
        conn.set("via", via)
    if tl:
        conn.set("tl", tl)
        if link_index:
            conn.set("linkIndex", link_index)
    _net_append_connection(root, conn)


def wire_colombo_south_connections(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """Snap Colombo south geometry, merge fragment internals, and wire TLS cluster exit."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    if colombo_south_edges_missing(root) or not colombo_south_wiring_needed(root):
        return False

    frag_net = net_path.with_suffix(".colombo_south.frag.net.xml")
    try:
        _netconvert_colombo_south_fragment(frag_net)
        frag_root = ET.parse(frag_net).getroot()
        delta = _colombo_south_anchor_delta(root, frag_root)
        if delta is None:
            print("warning: Colombo south wiring skipped — no anchor", file=sys.stderr)
            return False
        dx, dy = delta

        for lane_idx in range(2):
            l0 = _edge_lane_element(root, "597576896#0", lane_idx)
            l1 = _edge_lane_element(root, "597576896#1", lane_idx)
            if l0 is not None and l1 is not None:
                _snap_lane_endpoints(l0, l1)

        for lane_idx in range(2):
            l1 = _edge_lane_element(root, "597576896#1", lane_idx)
            l2 = _edge_lane_element(root, "597576896#2", lane_idx)
            if l1 is not None and l2 is not None:
                _snap_lane_endpoints(l1, l2)

        l2 = _edge_lane_element(root, "597576896#2", 0)
        lsec = _edge_lane_element(root, "139484443", 0)
        if l2 is not None and lsec is not None:
            _snap_lane_endpoints(l2, lsec)

        for edge in frag_root.findall("edge"):
            eid = edge.get("id") or ""
            if not any(eid.startswith(prefix) for prefix in COLOMBO_SOUTH_INTERNAL_EDGE_PREFIXES):
                continue
            if root.find(f'edge[@id="{eid}"]') is not None:
                continue
            internal = copy.deepcopy(edge)
            for lane in internal.findall("lane"):
                lane_shape = lane.get("shape")
                if lane_shape:
                    lane.set("shape", _transform_shape(lane_shape, dx, dy))
            _net_insert_before_connections(root, internal)

        for conn in frag_root.findall("connection"):
            from_e = conn.get("from") or ""
            to_e = conn.get("to") or ""
            if from_e.startswith(":") and not any(
                from_e.startswith(p) for p in COLOMBO_SOUTH_INTERNAL_EDGE_PREFIXES
            ):
                continue
            allowed_from = {
                "597576896#1",
                "597576896#2",
                ":7198983662_0",
                ":7198983662_2",
                ":31898616_0",
            }
            allowed_to = {"597576896#2", "139484443", "-597576896#1"}
            if from_e not in allowed_from and not from_e.startswith(":7198983662"):
                continue
            if to_e not in allowed_to and not to_e.startswith(":7198983662"):
                continue
            if to_e == "-597576896#1" and root.find('edge[@id="-597576896#1"]') is None:
                continue
            _add_net_connection(
                root,
                from_e,
                to_e,
                conn.get("fromLane", "0"),
                conn.get("toLane", "0"),
                via=conn.get("via", ""),
                dir_=conn.get("dir", "s"),
                state="M",
            )

        cluster_int = COLOMBO_SOUTH_CLUSTER_INTERNAL_EDGE
        if root.find(f'edge[@id="{cluster_int}"]') is None:
            int_edge = ET.Element("edge", id=cluster_int, function="internal")
            for lane_idx in range(2):
                l0 = _edge_lane_element(root, "597576896#0", lane_idx)
                l1 = _edge_lane_element(root, "597576896#1", lane_idx)
                if l0 is None or l1 is None:
                    continue
                p0 = _shape_points(l0.get("shape", ""))[-1]
                p1 = _shape_points(l1.get("shape", ""))[0]
                lane = ET.Element(
                    "lane",
                    id=f"{cluster_int}_{lane_idx}",
                    index=str(lane_idx),
                    disallow=l0.get("disallow", ""),
                    speed=l0.get("speed", "8.33"),
                    length=f"{((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2) ** 0.5:.2f}",
                    shape=_shape_string([p0, p1]),
                )
                int_edge.append(lane)
            _net_insert_before_connections(root, int_edge)

        cluster = root.find(f'junction[@id="{MOORHOUSE_COLOMBO_TLS_CLUSTER}"]')
        if cluster is not None:
            int_lanes = cluster.get("intLanes", "").split()
            for suffix in ("_0", "_1"):
                lid = f"{cluster_int}{suffix}"
                if lid not in int_lanes:
                    int_lanes.append(lid)
            cluster.set("intLanes", " ".join(int_lanes))
            inc = cluster.get("incLanes", "").split()
            for lid in ("597576896#1_0", "597576896#1_1"):
                if lid not in inc:
                    inc.append(lid)
            cluster.set("incLanes", " ".join(inc))

        _add_net_connection(
            root,
            "597576896#0",
            "597576896#1",
            "0",
            "0",
            via=f"{cluster_int}_0",
            state="M",
        )
        _add_net_connection(
            root,
            "597576896#0",
            "597576896#1",
            "1",
            "1",
            via=f"{cluster_int}_1",
            state="M",
        )
        _add_net_connection(
            root,
            cluster_int,
            "597576896#1",
            "0",
            "0",
            state="M",
        )
        _add_net_connection(
            root,
            cluster_int,
            "597576896#1",
            "1",
            "1",
            state="M",
        )

        prepared = net_path.with_suffix(".colombo_wire.tmp.xml")
        validated = net_path.with_suffix(".colombo_wire.out.xml")
        tree.write(prepared, encoding="UTF-8", xml_declaration=True)
        cmd = [
            sumo_bin("netconvert"),
            "--sumo-net-file",
            str(prepared),
            "--output-file",
            str(validated),
        ]
        detail = "Colombo south wiring"
        if prog is not None and prog.enabled:
            run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
        else:
            print("running:", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=str(ROOT))
        replace_net_file(validated, net_path)
        _ensure_colombo_south_junction_types(net_path)
        apply_colombo_south_segment_dir_fix(net_path)
    finally:
        frag_net.unlink(missing_ok=True)
        frag_net.with_suffix(".osm.xml").unlink(missing_ok=True)
        net_path.with_suffix(".colombo_wire.tmp.xml").unlink(missing_ok=True)
        net_path.with_suffix(".colombo_wire.out.xml").unlink(missing_ok=True)

    print(
        f"wired southbound Colombo through {MOORHOUSE_COLOMBO_TLS_CLUSTER} -> {net_path.name}"
    )
    return True


def finalize_colombo_south_at_moorhouse(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> None:
    """Import and connect southbound Colombo Street from the Moorhouse TLS cluster."""
    root = ET.parse(net_path).getroot()
    if colombo_south_edges_missing(root):
        patch_colombo_south_from_moorhouse_cluster(net_path, prog=prog)
    root = ET.parse(net_path).getroot()
    if not colombo_south_edges_missing(root):
        wire_colombo_south_connections(net_path, prog=prog)
    extend_colombo_south_114648686(net_path, prog=prog)
    extend_colombo_south_31946882(net_path, prog=prog)
    rebuild_colombo_south_geometry_from_osm(net_path, prog=prog)
    apply_colombo_south_segment_dir_fix(net_path)


def _ensure_colombo_south_junction_types(net_path: Path) -> None:
    """Keep intermediate Colombo nodes passable (not dead_end)."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    changed = False
    for jid in ("7198983662", "31898616", "31898617", "7198983663", "10970068712"):
        junc = root.find(f'junction[@id="{jid}"]')
        if junc is None:
            continue
        if junc.get("type") == "dead_end":
            junc.set("type", "priority")
            changed = True
    if changed:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)


def _revalidate_net(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
    detail: str = "net revalidate",
) -> None:
    tmp = net_path.with_suffix(".revalidate.tmp.xml")
    cmd = [
        sumo_bin("netconvert"),
        "--sumo-net-file",
        str(net_path),
        "--output-file",
        str(tmp),
    ]
    if prog is not None and prog.enabled:
        run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
    else:
        print("running:", " ".join(cmd))
        subprocess.run(cmd, check=True, cwd=str(ROOT))
    replace_net_file(tmp, net_path)


def import_colombo_south_from_moorhouse_cluster(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> bool:
    """Import southbound Colombo at Moorhouse cluster; rebuild TLS and border cleanup."""
    finalize_colombo_south_at_moorhouse(net_path, prog=prog)
    if colombo_south_patch_needed(ET.parse(net_path).getroot()):
        print("warning: Colombo south import incomplete", file=sys.stderr)
        return False
    finalize_clipped_border_junctions(net_path, prog=prog)
    finalize_actuated_tls(net_path, prog=prog)
    extra_edges = remove_dead_end_uturns(net_path, prog=prog)
    if extra_edges:
        print(f"removed {extra_edges} clipped-border edge(s) after Colombo south import")
    removed_conns = patch_dead_end_no_uturn(net_path)
    if removed_conns:
        print(f"removed {removed_conns} U-turn connection(s) after Colombo south import")
    apply_colombo_moorhouse_no_uturn(net_path)
    patch_colombo_moorhouse_passenger_through(net_path)
    return True


def patch_boundary_stub_dead_ends(net_path: Path) -> int:
    """Set listed stub junctions to type dead_end and drop U-turn pocket internals."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    by_id = {j.get("id"): j for j in root.findall("junction") if j.get("id")}
    patched = 0

    for jid in sorted(BOUNDARY_DEAD_END_JUNCTION_IDS):
        junc = by_id.get(jid)
        if junc is None:
            print(f"warning: boundary dead-end junction {jid} not in network", file=sys.stderr)
            continue
        prefix = f":{jid}_"
        for edge in list(root.findall("edge")):
            eid = edge.get("id") or ""
            if eid.startswith(prefix):
                root.remove(edge)
        for conn in list(root.findall("connection")):
            fe = conn.get("from") or ""
            te = conn.get("to") or ""
            via = conn.get("via") or ""
            if fe.startswith(prefix) or te.startswith(prefix) or via.startswith(prefix):
                root.remove(conn)
        for child in list(junc):
            junc.remove(child)
        junc.set("type", "dead_end")
        junc.set("intLanes", "")
        if "tl" in junc.attrib:
            del junc.attrib["tl"]
        junc.set("shape", _dead_end_shape(junc, root))
        patched += 1

    if patched:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"promoted {patched} stub junction(s) to dead_end -> {net_path.name}"
        )
    return patched


def patch_selwyn_street_boundary_geometry(net_path: Path) -> int:
    """
    Align Selwyn St clip stub lane shapes with dead_end junction 8871638711.

    Without this, vehicles on -1015757970#1 / 1015757970#1 render below the road.
    """
    tree = ET.parse(net_path)
    root = tree.getroot()
    junc = root.find(f".//junction[@id='{SELWYN_BOUNDARY_JUNCTION_ID}']")
    if junc is None:
        return 0
    anchor = (float(junc.get("x", 0)), float(junc.get("y", 0)))
    patched = 0

    for edge in root.findall("edge"):
        eid = edge.get("id") or ""
        if eid == SELWYN_BOUNDARY_SOURCE_EDGE:
            for lane in edge.findall("lane"):
                if _extend_lane_shape_endpoints(lane, prepend=anchor):
                    patched += 1
        elif eid == SELWYN_BOUNDARY_SINK_EDGE:
            for lane in edge.findall("lane"):
                if _extend_lane_shape_endpoints(lane, append=anchor):
                    patched += 1

    if patched:
        junc.set("shape", _dead_end_shape(junc, root))
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Selwyn St boundary: aligned {patched} lane shape(s) at "
            f"{SELWYN_BOUNDARY_JUNCTION_ID} -> {net_path.name}"
        )
        _revalidate_net(net_path, detail="Selwyn boundary geometry revalidate")
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
    return patched


def patch_selwyn_street_bus_depart_lane(net_path: Path) -> int:
    """Buses on -1015757970#1 must use lane 0 (straight); lane 1 turns right."""
    tree = ET.parse(net_path)
    root = tree.getroot()
    patched = 0
    for edge in root.findall("edge"):
        if edge.get("id") != SELWYN_BOUNDARY_SOURCE_EDGE:
            continue
        for lane in edge.findall("lane"):
            if (lane.get("index") or "") != "1":
                continue
            if _lane_xml_allows_bus(lane) and _lane_strip_bus_access(lane):
                patched += 1
    if patched:
        if hasattr(ET, "indent"):
            ET.indent(root, space="    ")
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
        print(
            f"Selwyn St boundary: removed bus from {patched} right-turn lane(s) "
            f"on {SELWYN_BOUNDARY_SOURCE_EDGE} -> {net_path.name}"
        )
        global _NET_LANE_PERM_CACHE
        _NET_LANE_PERM_CACHE = None
    return patched


def patch_dead_end_no_uturn(net_path: Path) -> int:
    """Remove turnaround connections at boundary and clipped dead-end junctions."""
    tree = ET.parse(net_path)
    root = tree.getroot()

    dead = _dead_end_junction_ids(root)
    edge_ends: dict[str, tuple[str, str]] = {}
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        edge_ends[eid] = (edge.get("from") or "", edge.get("to") or "")

    feeder_juncs = {edge_ends[e][0] for e in edge_ends if edge_ends[e][1] in dead}
    removed = 0
    for conn in list(root.findall("connection")):
        from_edge = conn.get("from") or ""
        to_edge = conn.get("to") or ""
        via = conn.get("via") or ""
        drop = False
        int_junc = (
            _junction_from_internal_id(from_edge)
            or _junction_from_internal_id(to_edge)
            or _junction_from_internal_id(via)
        )
        if int_junc in dead:
            drop = True
        elif from_edge in edge_ends:
            from_junc, to_junc = edge_ends[from_edge]
            to_conn_junc = edge_ends[to_edge][0] if to_edge in edge_ends else ""
            if conn.get("dir") == "T":
                if from_junc in dead:
                    drop = True
                elif to_junc in dead or to_conn_junc in feeder_juncs:
                    drop = True
            elif (
                to_junc in dead
                and to_edge in edge_ends
                and _edge_is_reverse(from_edge, to_edge)
            ):
                drop = True
        if drop:
            root.remove(conn)
            removed += 1

    if removed:
        tree.write(net_path, encoding="UTF-8", xml_declaration=True)
    return removed


def find_clipped_border_edges_to_remove(net_path: Path) -> list[str]:
    """U-turn legs and dead_end-to-dead_end links at clipped borders."""
    root = ET.parse(net_path).getroot()
    return sorted(
        set(
            _dead_end_uturn_edges_from_root(root)
            + _dead_end_connector_edges_from_root(root)
        )
    )


def find_dead_end_uturn_edges(net_path: Path) -> list[str]:
    """Outgoing edges at dead_end junctions that reverse back on the same street."""
    return _dead_end_uturn_edges_from_root(ET.parse(net_path).getroot())


def _dead_end_uturn_edges_from_root(root: ET.Element) -> list[str]:
    """Outgoing edges at clipped dead_end junctions that reverse back on the same street."""
    dead = _clipped_dead_end_junction_ids(root)
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    for edge in root.findall("edge"):
        if edge.get("function"):
            continue
        eid = edge.get("id") or ""
        if eid.startswith(":"):
            continue
        fr, to = edge.get("from"), edge.get("to")
        if to in dead:
            incoming.setdefault(to, []).append(eid)
        if fr in dead:
            outgoing.setdefault(fr, []).append(eid)

    to_remove: list[str] = []
    for jid in dead:
        for out_id in outgoing.get(jid, []):
            for in_id in incoming.get(jid, []):
                if _edge_base(in_id) == _edge_base(out_id) and in_id != out_id:
                    to_remove.append(out_id)
                    break
    return sorted(set(to_remove))


def remove_dead_end_uturns(
    net_path: Path,
    max_rounds: int = 10,
    *,
    prog: PipelineStepProgress | None = None,
) -> int:
    """Strip reverse-direction legs at clipped dead_end junctions (no U-turn pockets)."""
    removed_total = 0
    for round_i in range(1, max_rounds + 1):
        edges = find_clipped_border_edges_to_remove(net_path)
        if not edges:
            break
        tmp = net_path.with_suffix(f".nouturn{round_i}.tmp.xml")
        cmd = [
            sumo_bin("netconvert"),
            "--sumo-net-file",
            str(net_path),
            "--output-file",
            str(tmp),
            "--remove-edges.explicit",
            ",".join(edges),
        ]
        detail = f"U-turn removal {round_i}"
        if prog is not None and prog.enabled:
            run_subprocess_with_progress(cmd, prog, detail, cwd=ROOT)
        else:
            print("running:", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=str(ROOT))
        replace_net_file(tmp, net_path)
        removed_total += len(edges)
        print(
            f"  dead_end U-turn removal round {round_i}: "
            f"removed {len(edges)} edge(s)"
        )
    else:
        print(
            f"warning: dead_end U-turn removal stopped after {max_rounds} rounds",
            file=sys.stderr,
        )

    if removed_total:
        print(
            f"removed {removed_total} clipped-border edge(s) total -> {net_path.name}"
        )
    return removed_total


def import_roundabout_seeds(
    net_path: Path,
    *,
    osm_in: Path = OSM_IN,
    osm_out: Path = OSM_OUT,
    prog: PipelineStepProgress | None = None,
) -> int:
    """
    Import OSM roundabout ring(s) for ROUNDABOUT_SEED_JUNCTION_IDS and rebuild net.

    Updates filtered OSM if needed, then netconvert + standard network post-processing.
    """
    added = enrich_filtered_osm_roundabouts(osm_in, osm_out, ROUNDABOUT_SEED_JUNCTION_IDS)
    if added:
        print(f"added {added} roundabout OSM way(s) -> {osm_out.name}")
    elif not find_roundabout_osm_ways(osm_out, ROUNDABOUT_SEED_JUNCTION_IDS)[0]:
        print(
            "warning: no roundabout OSM ways found for seed junctions",
            file=sys.stderr,
        )

    run_netconvert(osm_out, net_path, prog=prog, detail="netconvert (roundabout)")
    apply_junction_joins(net_path, prog=prog)
    finalize_clipped_border_junctions(net_path, prog=prog)
    finalize_actuated_tls(net_path, prog=prog)
    extra_edges = remove_dead_end_uturns(net_path, prog=prog)
    if extra_edges:
        print(f"removed {extra_edges} clipped-border edge(s) after roundabout import")
    removed_conns = patch_dead_end_no_uturn(net_path)
    if removed_conns:
        print(f"removed {removed_conns} U-turn connection(s) after roundabout import")
    return added


def import_opposite_clip_edges(
    net_path: Path,
    *,
    osm_out: Path = OSM_OUT,
    prog: PipelineStepProgress | None = None,
) -> int:
    """
    Import reverse-direction edge(s) for BIDIRECTIONAL_CLIP_OSM_WAY_IDS (e.g. -1015757969#0).

    Strips oneway in filtered OSM, rebuilds net, and keeps both directions at boundary stubs.
    """
    patched = enrich_filtered_osm_bidirectional_clip_ways(osm_out)
    if patched:
        print(f"removed oneway from {patched} OSM way(s) -> {osm_out.name}")

    run_netconvert(osm_out, net_path, prog=prog, detail="netconvert (bidirectional clip)")
    apply_junction_joins(net_path, prog=prog)
    finalize_clipped_border_junctions(net_path, prog=prog)
    finalize_actuated_tls(net_path, prog=prog)
    extra_edges = remove_dead_end_uturns(net_path, prog=prog)
    if extra_edges:
        print(f"removed {extra_edges} clipped-border edge(s) after opposite-edge import")
    removed_conns = patch_dead_end_no_uturn(net_path)
    if removed_conns:
        print(f"removed {removed_conns} U-turn connection(s) after opposite-edge import")
    return patched


def finalize_clipped_border_junctions(
    net_path: Path,
    *,
    prog: PipelineStepProgress | None = None,
) -> None:
    """Promote boundary stubs to dead_end; strip clipped-border U-turns."""
    patch_boundary_stub_dead_ends(net_path)
    patch_riccarton_west_lane_geometry(net_path, prog=prog)
    finalize_colombo_south_at_moorhouse(net_path, prog=prog)
    removed_edges = remove_dead_end_uturns(net_path, prog=prog)
    removed_conns = patch_dead_end_no_uturn(net_path)
    apply_colombo_moorhouse_no_uturn(net_path)
    patch_colombo_moorhouse_passenger_through(net_path)
    excluded_edges = remove_excluded_net_edges(net_path, prog=prog)
    print(
        f"cleared clipped border junctions: {removed_edges} edge(s), "
        f"{removed_conns} connection(s), {excluded_edges} excluded edge(s) "
        f"-> {net_path.name}"
    )


def step_network(args) -> int:
    print("=== Step: network ===")
    for path in (MAIN_STREETS, OSM_IN):
        if not path.is_file():
            print("missing:", path, file=sys.stderr)
            return 1

    prog = step_progress("NETWORK", args)
    phases = 10
    prog.tick("main streets", 1, phases)
    allowed = load_main_streets()
    print(f"main streets: {len(allowed)}")

    prog.tick("scan OSM", 2, phases)
    way_ids, node_ids = find_matching_ways(OSM_IN, allowed)
    rb_ways, rb_nodes = find_roundabout_osm_ways(OSM_IN, ROUNDABOUT_SEED_JUNCTION_IDS)
    way_ids |= rb_ways
    node_ids |= rb_nodes
    bi_ways, bi_nodes = find_osm_ways_by_ids(OSM_IN, BUS_INTERCHANGE_OSM_WAY_IDS)
    way_ids |= bi_ways
    node_ids |= bi_nodes
    bl_ways, bl_nodes = find_osm_extra_bus_lane_ways(OSM_IN, node_ids)
    way_ids |= bl_ways
    node_ids |= bl_nodes
    dropped = len(way_ids & EXCLUDED_OSM_WAY_IDS)
    way_ids = filter_excluded_osm_ways(way_ids)
    if dropped:
        print(f"excluded OSM ways: {dropped} ({', '.join(sorted(EXCLUDED_OSM_WAY_IDS))})")
    print(f"matching highway ways: {len(way_ids)}")
    print(f"nodes referenced: {len(node_ids)}")
    if rb_ways:
        print(
            f"roundabout OSM ways: {len(rb_ways)} "
            f"({', '.join(sorted(rb_ways))})"
        )
    if bi_ways:
        print(
            f"bus interchange OSM ways: {len(bi_ways)} "
            f"({', '.join(sorted(bi_ways))})"
        )
    elif BUS_INTERCHANGE_OSM_WAY_IDS:
        print(
            "warning: no bus interchange OSM ways found",
            file=sys.stderr,
        )
    if bl_ways:
        print(
            f"bus lane OSM ways: {len(bl_ways)} "
            f"({', '.join(sorted(bl_ways))})"
        )

    prog.tick("write OSM", 3, phases)
    write_filtered_osm(OSM_IN, OSM_OUT, way_ids, node_ids)
    patch_filtered_osm_drop_ways(OSM_OUT, EXCLUDED_OSM_WAY_IDS)
    bidir = enrich_filtered_osm_bidirectional_clip_ways(OSM_OUT)
    if bidir:
        print(f"bidirectional clip ways (oneway removed): {bidir}")
    print(f"wrote filtered OSM -> {OSM_OUT}")

    prog.tick("netconvert", 4, phases)
    run_netconvert(OSM_OUT, NET_XML, prog=prog, detail="netconvert")
    print(f"wrote SUMO network -> {NET_XML}")

    prog.tick("junction joins", 5, phases)
    apply_junction_joins(NET_XML, prog=prog)

    prog.tick("border cleanup", 6, phases)
    finalize_clipped_border_junctions(NET_XML, prog=prog)
    prog.tick("TLS + link slips", 7, phases)
    finalize_actuated_tls(NET_XML, prog=prog)
    # tls.rebuild can restore border U-turn edges; strip again (connections only once)
    prog.tick("border re-check", 9, phases)
    extra_edges = remove_dead_end_uturns(NET_XML, prog=prog)
    if extra_edges:
        print(f"removed {extra_edges} clipped-border edge(s) after TLS rebuild")
    removed_conns = patch_dead_end_no_uturn(NET_XML)
    if removed_conns:
        print(f"removed {removed_conns} U-turn connection(s) after TLS rebuild")
    apply_colombo_moorhouse_no_uturn(NET_XML)
    patch_colombo_moorhouse_passenger_through(NET_XML)
    patch_selwyn_street_boundary_geometry(NET_XML)
    patch_selwyn_street_bus_depart_lane(NET_XML)
    prog.tick("bus interchange", 10, phases)
    patch_bus_interchange_turn_restrictions(NET_XML)
    patch_bus_interchange_portal_connections(NET_XML)
    patch_bus_no_uturn_connections(NET_XML)
    patch_cbd_30kph_bus_corridors(NET_XML)
    patch_lichfield_bus_manchester_connector(NET_XML)
    patch_colombo_street_bus_corridor(NET_XML)
    patch_riccarton_avenue_bus_corridor(NET_XML)
    patch_colombo_lichfield_bus_depart(NET_XML)
    patch_lichfield_colombo_left_internal_bus(NET_XML)
    patch_busway_left_bicycle_only_lanes(NET_XML)
    patch_tuam_street_junction_bus_access(NET_XML)
    patch_saint_asaph_bus_lane_entrance(NET_XML)
    remove_false_opposite_busway_backward_edges(NET_XML)
    patch_tuam_bus_lane_turn_restrictions(NET_XML)
    patch_hagley_avenue_bus_to_tuam_restriction(NET_XML)
    patch_high_street_no_bus(NET_XML)
    patch_bus_forbidden_cbd_edges(NET_XML)
    patch_internal_bicycle_only_junction_lanes(NET_XML)
    patch_bus_interchange_speed_limit(NET_XML)
    global _NET_LANE_PERM_CACHE
    _NET_LANE_PERM_CACHE = None  # net changed; refresh on next route check
    prog.dismiss()
    prog.finish("COMPLETED: network")
    return 0


# --- Step 2: map intersections -----------------------------------------------

def is_drivable_normal(edge) -> bool:
    fn = edge.getFunction()
    if fn in ("internal", "crossing", "walkingarea", "connector"):
        return False
    eid = edge.getID()
    if eid.startswith(":"):
        return False
    if not any(lane.allows("passenger") for lane in edge.getLanes()):
        return False
    return True


def pick_best(scored: list[tuple]) -> tuple:
    return scored[0]


def edge_dist_to_xy(edge, x: float, y: float) -> float:
    shape = edge.getShape()
    if not shape:
        nx, ny = edge.getFromNode().getCoord()
        return ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
    return min(((px - x) ** 2 + (py - y) ** 2) ** 0.5 for px, py in shape)


def pick_enter_exit_edges(
    net,
    junction_id: str,
    x: float,
    y: float,
) -> tuple[str, str]:
    """Enter = incoming edge to junction; exit = outgoing (vehicles pass through)."""
    node = net.getNode(junction_id)
    incoming = [e for e in node.getIncoming() if is_drivable_normal(e)]
    outgoing = [e for e in node.getOutgoing() if is_drivable_normal(e)]
    if not incoming or not outgoing:
        return "", ""
    incoming.sort(key=lambda e: edge_dist_to_xy(e, x, y))
    outgoing.sort(key=lambda e: edge_dist_to_xy(e, x, y))
    return incoming[0].getID(), outgoing[0].getID()


def pick_main_junction(net, x: float, y: float, fallback_junction_id: str) -> str:
    """Prefer a multi-leg junction near the survey point over a nearby spur link."""
    try:
        fallback = net.getNode(fallback_junction_id)
        fallback_legs = sum(
            1 for e in fallback.getIncoming() if is_drivable_normal(e)
        )
    except Exception:
        fallback_legs = 0
    if fallback_legs >= 3:
        return fallback_junction_id

    best_id = fallback_junction_id
    best_legs = fallback_legs
    best_dist = float("inf")
    seen: set[str] = set()
    for lane, _dist in net.getNeighboringLanes(x, y, SEARCH_RADIUS_M):
        edge = lane.getEdge()
        for jid in (edge.getToNode().getID(), edge.getFromNode().getID()):
            if jid in seen:
                continue
            seen.add(jid)
            try:
                node = net.getNode(jid)
            except Exception:
                continue
            legs = sum(1 for e in node.getIncoming() if is_drivable_normal(e))
            if legs < 3:
                continue
            jx, jy = node.getCoord()
            jdist = ((jx - x) ** 2 + (jy - y) ** 2) ** 0.5
            if legs > best_legs or (legs == best_legs and jdist < best_dist):
                best_legs = legs
                best_dist = jdist
                best_id = jid
    return best_id


def step_map(args) -> int:
    print("=== Step: map ===")
    for path in (NET_XML, INTERSECTION_CSV):
        if not path.is_file():
            print("missing:", path, file=sys.stderr)
            if path == NET_XML:
                print("Run the network step first.", file=sys.stderr)
            return 1

    setup_sumolib()
    import sumolib.net as sumo_net  # noqa: E402

    prog = step_progress("MAPPING", args)
    prog.tick("load network", 0, 1)
    n = sumo_net.readNet(str(NET_XML), withInternal=False)
    with INTERSECTION_CSV.open(newline="", encoding="utf-8") as f:
        intersection_rows = list(csv.DictReader(f))
    rows_out = []
    total = len(intersection_rows)
    for idx, row in enumerate(intersection_rows, start=1):
        prog.tick(row["intersection_id"].strip() or "—", idx, total)
        iid = row["intersection_id"].strip()
        lon = float(row["longitude"])
        lat = float(row["latitude"])
        x, y = n.convertLonLat2XY(lon, lat)
        lanes = n.getNeighboringLanes(x, y, SEARCH_RADIUS_M)
        scored = []
        for lane, dist in lanes:
            e = lane.getEdge()
            if not is_drivable_normal(e):
                continue
            scored.append((dist, e.getID(), lane.getID(), e.getToNode().getID()))
        scored.sort(key=lambda t: t[0])
        scored = scored[:MAX_CANDIDATES]
        if not scored:
            rows_out.append(
                {
                    "intersection_id": iid,
                    "longitude": lon,
                    "latitude": lat,
                    "net_x": round(x, 3),
                    "net_y": round(y, 3),
                    "nearest_edge": "",
                    "enter_edge": "",
                    "exit_edge": "",
                    "nearest_lane": "",
                    "dist_m": "",
                    "to_junction": "",
                    "candidates": "",
                }
            )
            continue
        best = pick_best(scored)
        main_junction = pick_main_junction(n, x, y, best[3])
        enter_edge, exit_edge = pick_enter_exit_edges(n, main_junction, x, y)
        if not enter_edge:
            enter_edge = best[1]
        if not exit_edge:
            exit_edge = best[1]
        cand = ";".join(f"{ed}:{round(d, 2)}m" for d, ed, _, __ in scored)
        rows_out.append(
            {
                "intersection_id": iid,
                "longitude": lon,
                "latitude": lat,
                "net_x": round(x, 3),
                "net_y": round(y, 3),
                "nearest_edge": best[1],
                "enter_edge": enter_edge,
                "exit_edge": exit_edge,
                "nearest_lane": best[2],
                "dist_m": round(best[0], 3),
                "to_junction": main_junction,
                "candidates": cand,
            }
        )

    fieldnames = [
        "intersection_id",
        "longitude",
        "latitude",
        "net_x",
        "net_y",
        "nearest_edge",
        "enter_edge",
        "exit_edge",
        "nearest_lane",
        "dist_m",
        "to_junction",
        "candidates",
    ]
    with EDGE_MAP_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows_out)
    print(f"Network: {NET_XML.name}")
    print(f"Wrote {len(rows_out)} rows to {EDGE_MAP_CSV}")
    prog.dismiss()
    prog.finish("COMPLETED: map")
    return 0


# --- Step 3: traffic -> trips ------------------------------------------------

def newest_traffic_csv() -> Path:
    """Pick the newest parsed traffic_*.csv (filename timestamp, else mtime)."""
    files = glob.glob(TRAFFIC_GLOB)
    if not files:
        raise FileNotFoundError(f"No file matching {TRAFFIC_GLOB}")

    def sort_key(path_str: str) -> datetime:
        name = Path(path_str).name
        m = re.match(r"traffic_(\d{2})([A-Za-z]{3})(\d{4})_(\d{6})\.csv$", name, re.I)
        if m:
            try:
                return datetime.strptime(
                    f"{m.group(1)}{m.group(2).title()}{m.group(3)}_{m.group(4)}",
                    "%d%b%Y_%H%M%S",
                )
            except ValueError:
                pass
        return datetime.fromtimestamp(os.path.getmtime(path_str))

    return Path(max(files, key=sort_key))


def load_edge_maps(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    enter_map, exit_map, _, _ = load_intersection_maps(path)
    return enter_map, exit_map


def load_junction_map(path: Path) -> dict[str, str]:
    _, _, junction_map, _ = load_intersection_maps(path)
    return junction_map


def load_intersection_maps(
    path: Path,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, tuple[float, float]],
]:
    enter_map: dict[str, str] = {}
    exit_map: dict[str, str] = {}
    junction_map: dict[str, str] = {}
    coord_map: dict[str, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iid = row["intersection_id"].strip()
            if not iid:
                continue
            fallback = row.get("nearest_edge", "").strip()
            enter = row.get("enter_edge", "").strip() or fallback
            exit_ = row.get("exit_edge", "").strip() or fallback
            if enter:
                enter_map[iid] = enter
            if exit_:
                exit_map[iid] = exit_
            junction = row.get("to_junction", "").strip()
            if junction:
                junction_map[iid] = junction
            try:
                coord_map[iid] = (float(row["net_x"]), float(row["net_y"]))
            except (KeyError, TypeError, ValueError):
                pass
    return enter_map, exit_map, junction_map, coord_map


def edge_compass_label(edge) -> str:
    shape = edge.getShape()
    if len(shape) < 2:
        return ""
    x1, y1 = shape[0]
    x2, y2 = shape[-1]
    dx, dy = x2 - x1, y2 - y1
    if abs(dx) < 0.5 and abs(dy) < 0.5:
        return ""
    if abs(dy) >= abs(dx):
        return "northbound" if dy > 0 else "southbound"
    return "eastbound" if dx > 0 else "westbound"


def _edge_unit_vector(edge) -> tuple[float, float]:
    shape = edge.getShape()
    if len(shape) < 2:
        return (0.0, 0.0)
    x1, y1 = shape[0]
    x2, y2 = shape[-1]
    dx, dy = x2 - x1, y2 - y1
    mag = (dx * dx + dy * dy) ** 0.5
    if mag < 1e-6:
        return (0.0, 0.0)
    return (dx / mag, dy / mag)


def is_boundary_source_edge(net, edge_id: str) -> bool:
    """True if edge leaves the network at a clipped dead-end (trip from= edge)."""
    if not edge_id:
        return False
    try:
        return net.getEdge(edge_id).getFromNode().getType() == "dead_end"
    except Exception:
        return False


def is_boundary_sink_edge(net, edge_id: str) -> bool:
    """True if edge ends at a clipped dead-end (trip to= edge)."""
    if not edge_id:
        return False
    try:
        return net.getEdge(edge_id).getToNode().getType() == "dead_end"
    except Exception:
        return False


def _outgoing_edge_ids(net, edge_id: str) -> list[str]:
    """Edges reachable in one step from edge_id (lane connections, not whole junction)."""
    try:
        return [
            out.getID()
            for out in net.getEdge(edge_id).getOutgoing()
            if is_drivable_normal(out)
        ]
    except Exception:
        return []


def edge_connected(net, from_edge: str, to_edge: str) -> bool:
    """True if vehicles may drive directly from one edge to the next."""
    if not from_edge or not to_edge:
        return False
    if from_edge == to_edge:
        return True
    return to_edge in _outgoing_edge_ids(net, from_edge)


def edge_connected_vclass(
    net,
    from_edge: str,
    to_edge: str,
    vclass: str = "passenger",
    *,
    allow_uturn: bool = True,
) -> bool:
    """True if vclass may drive directly from one edge to the next."""
    if not from_edge or not to_edge:
        return False
    if from_edge == to_edge:
        return True
    return _lane_pair_connected_vclass(
        net, from_edge, to_edge, vclass, allow_uturn=allow_uturn
    )


def _lane_pair_connected_vclass(
    net,
    from_edge: str,
    to_edge: str,
    vclass: str = "passenger",
    *,
    allow_uturn: bool = True,
) -> bool:
    """True when a lane allowing vclass can reach to_edge on a lane allowing vclass."""
    try:
        from_e = net.getEdge(from_edge)
    except Exception:
        return False
    for from_lane in from_e.getLanes():
        if not from_lane.allows(vclass):
            continue
        for conn in from_lane.getOutgoing():
            if not allow_uturn and _sumo_connection_is_uturn(conn):
                continue
            if not _sumo_connection_allows_vclass(conn, vclass):
                continue
            try:
                to_lane = conn.getToLane()
            except Exception:
                continue
            if to_lane.getEdge().getID() == to_edge:
                return True
    return False


def _strip_false_opposite_busway_backward_hops(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """
    Drop spurious -23151049#* hops; splice vclass-feasible paths instead.

    Routes must not reference the OSM opposite_lane artifact as a lane/edge id
    (only -23151049#1_0 exists; viewers often fail on -23151049#1).
    """
    if not edges:
        return []
    out: list[str] = []
    i = 0
    while i < len(edges):
        eid = edges[i]
        if not _false_opposite_busway_backward_edge(eid):
            out.append(eid)
            i += 1
            continue
        fwd = eid[1:]
        nxt = edges[i + 1] if i + 1 < len(edges) else None
        if nxt == fwd:
            i += 2
            if not out:
                out.append(TUAM_BUS_LANE_JUNCTION_EDGE if vclass == "bus" else fwd)
            elif not edge_connected_vclass(net, out[-1], fwd, vclass):
                path = shortest_vclass_edge_path(net, out[-1], fwd, vclass)
                if not path or len(path) < 2:
                    return []
                out.extend(path[1:])
            else:
                out.append(fwd)
            continue
        target = nxt or fwd
        if not out:
            start = TUAM_BUS_LANE_JUNCTION_EDGE if vclass == "bus" else fwd
            if target == fwd:
                out.append(start)
                i += 1
                continue
            path = shortest_vclass_edge_path(net, start, target, vclass)
            if not path:
                return []
            out.extend(path)
            i += 2 if nxt else 1
            continue
        path = shortest_vclass_edge_path(net, out[-1], target, vclass)
        if not path or len(path) < 2:
            return []
        out.extend(path[1:])
        i += 2 if nxt else 1
    return out


def _pick_saint_asaph_exit_tail(rest: list[str]) -> tuple[str, ...]:
    """Choose Riccarton vs Hagley exit after Tuam St Bus Lane from downstream hints."""
    hint_set = set(rest)
    hagley_score = len(hint_set & HAGLEY_ROUTE_HINTS)
    ric_score = len(hint_set & RICCARTON_ROUTE_HINTS)
    if ric_score > hagley_score:
        return SAINT_ASAPH_TO_RICCARTON_TAIL
    if hagley_score > ric_score:
        return SAINT_ASAPH_TO_HAGLEY_TAIL
    if "777634282#1" in rest and "479999311#0" in rest:
        return SAINT_ASAPH_TO_RICCARTON_TAIL
    if "22779530#0" in rest:
        return SAINT_ASAPH_TO_HAGLEY_TAIL
    return SAINT_ASAPH_TO_HAGLEY_TAIL


def _saint_asaph_bus_lane_corridor(
    net,
    rest: list[str],
    vclass: str,
) -> list[str] | None:
    """Full Saint Asaph -> bus lane -> Riccarton or Hagley corridor."""
    tail = _pick_saint_asaph_exit_tail(rest)
    corridor = list(SAINT_ASAPH_BUS_LANE_PREFIX) + list(tail)
    if _route_edges_connected(net, corridor, vclass):
        return corridor
    return None


def _saint_asaph_entered_bus_lane(edges: list[str], i: int) -> bool:
    """True when 1015728534#0 is followed by the Tuam St Bus Lane (344479221#0-#4)."""
    if i >= len(edges) or edges[i] != SAINT_ASAPH_WEST_EDGE:
        return False
    offset = 1
    if i + 1 < len(edges) and edges[i + 1] == SAINT_ASAPH_BUS_TURN_EDGE:
        offset = 2
    start = i + offset
    seq = list(TUAM_BUS_LANE_SEQUENCE)
    return edges[start : start + len(seq)] == seq


def _saint_asaph_to_tuam_east_path(net, start_edge: str, vclass: str) -> list[str]:
    """Bus-feasible path to Tuam east that avoids BUS_FORBIDDEN_EDGES."""
    return shortest_vclass_edge_path_avoiding(
        net,
        start_edge,
        TUAM_EAST_EDGE,
        BUS_FORBIDDEN_EDGES,
        vclass,
        allow_uturn=vclass_allow_uturn(vclass),
    )


def _skip_after_saint_asaph_without_bus_lane(edges: list[str], start: int) -> int:
    """Index of the first edge after wrong Saint Asaph / Antigua hops."""
    j = start
    while j < len(edges):
        e = edges[j]
        if e in SAINT_ASAPH_BUS_LANE_BYPASS_EDGES or e == SAINT_ASAPH_CONTINUE_EDGE:
            j += 1
            continue
        if e == SAINT_ASAPH_BUS_TURN_EDGE:
            nxt = edges[j + 1] if j + 1 < len(edges) else ""
            if nxt == TUAM_BUS_LANE_START_EDGE:
                break
            j += 1
            continue
        if e in SAINT_ASAPH_WRONG_TURN_EDGES:
            j += 1
            continue
        break
    return j


def _preservable_saint_asaph_end(
    net,
    edges: list[str],
    i: int,
    vclass: str,
    *,
    max_hops: int = 30,
) -> int | None:
    """
    When edges[i] is Saint Asaph and the OSM continuation is already bus-valid
    (Tuam east, Rangiora return, etc.), return the index after that run.
    """
    if i + 1 >= len(edges) or edges[i] != SAINT_ASAPH_WEST_EDGE:
        return None
    if _saint_asaph_entered_bus_lane(edges, i):
        return None
    j = i + 1
    while j < len(edges) and j - i <= max_hops:
        if not _lane_pair_connected_vclass(net, edges[j - 1], edges[j], vclass):
            return None
        e = edges[j]
        if e in BUS_FORBIDDEN_EDGES:
            return None
        if e in (SAINT_ASAPH_CONTINUE_EDGE, TUAM_BUS_LANE_START_EDGE):
            return None
        if e in TUAM_BUS_LANE_SEQUENCE:
            return None
        j += 1
    return j if j > i + 1 else None


def _redirect_saint_asaph_straight_to_bus_lane(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """
    All buses on Saint Asaph (1015728534#0) turn onto Tuam St Bus Lane 344479221#0-#4.

    Replaces straight-ahead Saint Asaph hops, wrong Antigua turns, and other bypasses.
    """
    if vclass != "bus" or len(edges) < 2:
        return edges
    out: list[str] = []
    i = 0
    while i < len(edges):
        if edges[i] == SAINT_ASAPH_WEST_EDGE:
            if _saint_asaph_entered_bus_lane(edges, i):
                out.append(edges[i])
                i += 1
                continue
            preserve_end = _preservable_saint_asaph_end(net, edges, i, vclass)
            if preserve_end is not None:
                out.extend(edges[i:preserve_end])
                i = preserve_end
                continue
            j = _skip_after_saint_asaph_without_bus_lane(edges, i + 1)
            rest = edges[j:]
            if TUAM_EAST_EDGE in rest:
                path = _saint_asaph_to_tuam_east_path(net, SAINT_ASAPH_WEST_EDGE, vclass)
                if path:
                    out.extend(path)
                    k = j
                    while k < len(edges) and edges[k] != TUAM_EAST_EDGE:
                        k += 1
                    i = k + 1 if k < len(edges) else j
                    continue
            corridor = _saint_asaph_bus_lane_corridor(net, rest, vclass)
            if corridor:
                out.extend(corridor)
                i = j
                continue
        out.append(edges[i])
        i += 1
    return out


def _repair_tuam_bus_lane_junction_exits(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """After 344479221#4, enforce Riccarton or Hagley exit corridor."""
    if vclass != "bus" or TUAM_BUS_LANE_JUNCTION_EDGE not in edges:
        return edges
    idx = edges.index(TUAM_BUS_LANE_JUNCTION_EDGE)
    rest = edges[idx + 1 :]
    tail = _pick_saint_asaph_exit_tail(rest)
    corridor_tail = list(tail)
    if rest[: len(corridor_tail)] == corridor_tail:
        return edges
    if not _route_edges_connected(
        net, [TUAM_BUS_LANE_JUNCTION_EDGE, *corridor_tail], vclass
    ):
        return edges
    j = idx + 1
    while j < len(edges) and edges[j] in SAINT_ASAPH_BUS_LANE_BYPASS_EDGES:
        j += 1
    return edges[: idx + 1] + corridor_tail + edges[j:]


def _repair_cbd_bus_corridors(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """Enforce Riccarton/Hagley <-> Tuam bus corridors on stitched routes."""
    if vclass != "bus" or len(edges) < 2:
        return edges

    edges = _repair_tuam_bus_lane_junction_exits(net, edges, vclass)

    out: list[str] = []
    i = 0
    while i < len(edges):
        eid = edges[i]
        if (
            eid == RICCARTON_TO_TUAM_CORRIDOR[0]
            and _route_edges_connected(net, RICCARTON_TO_TUAM_CORRIDOR, vclass)
        ):
            j = i + 1
            while j < len(edges) and edges[j] in SAINT_ASAPH_BUS_LANE_BYPASS_EDGES:
                j += 1
            if j == i + 1 and edges[i + 1] == RICCARTON_TO_TUAM_CORRIDOR[1]:
                j = i + 2
            if j < len(edges) and edges[j] == RICCARTON_TO_TUAM_CORRIDOR[-1]:
                out.extend(RICCARTON_TO_TUAM_CORRIDOR)
                i = j + 1
                continue
            if j > i + 1 or (
                i + len(RICCARTON_TO_TUAM_CORRIDOR) <= len(edges)
                and edges[i : i + len(RICCARTON_TO_TUAM_CORRIDOR)]
                != list(RICCARTON_TO_TUAM_CORRIDOR)
            ):
                out.extend(RICCARTON_TO_TUAM_CORRIDOR)
                while i < len(edges) and (
                    edges[i] in RICCARTON_TO_TUAM_CORRIDOR
                    or edges[i] in SAINT_ASAPH_BUS_LANE_BYPASS_EDGES
                ):
                    i += 1
                continue
        if (
            eid == HAGLEY_TO_TUAM_CORRIDOR[0]
            and _route_edges_connected(net, HAGLEY_TO_TUAM_CORRIDOR, vclass)
        ):
            if i + 1 < len(edges) and edges[i + 1] == HAGLEY_TO_TUAM_CORRIDOR[1]:
                out.extend(HAGLEY_TO_TUAM_CORRIDOR)
                i += 2
                continue
            out.extend(HAGLEY_TO_TUAM_CORRIDOR)
            i += 1
            while i < len(edges) and edges[i] in HAGLEY_TO_TUAM_CORRIDOR:
                i += 1
            continue
        if (
            eid == HAGLEY_AVE_EAST_EDGE
            and _route_edges_connected(net, list(HAGLEY_AVE_TO_TUAM_PAIR), vclass)
        ):
            if i + 1 < len(edges) and edges[i + 1] == HAGLEY_AVE_TO_TUAM_EXIT:
                out.extend(HAGLEY_AVE_TO_TUAM_PAIR)
                i += 2
                continue
            out.extend(HAGLEY_AVE_TO_TUAM_PAIR)
            i += 1
            while i < len(edges) and (
                edges[i] in HAGLEY_AVE_BUS_FORBIDDEN_EXITS
                or edges[i] in HAGLEY_AVE_TO_TUAM_PAIR
            ):
                i += 1
            continue
        out.append(eid)
        i += 1
    return out


def _repair_tuam_bus_lane_forbidden_exits(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """Replace blocked right / U-turn exits after 344479221#4 with bus-feasible paths."""
    if vclass != "bus" or len(edges) < 2:
        return edges
    allow_uturn = vclass_allow_uturn(vclass)
    out: list[str] = []
    i = 0
    while i < len(edges):
        if (
            edges[i] == TUAM_BUS_LANE_JUNCTION_EDGE
            and i + 1 < len(edges)
            and edges[i + 1] in TUAM_BUS_LANE_FORBIDDEN_EXITS
        ):
            j = i + 1
            while j < len(edges) and edges[j] in TUAM_BUS_LANE_FORBIDDEN_EXITS:
                j += 1
            target = edges[j] if j < len(edges) else edges[i + 1]
            path = shortest_vclass_edge_path(
                net,
                TUAM_BUS_LANE_JUNCTION_EDGE,
                target,
                vclass,
                allow_uturn=allow_uturn,
            )
            if path and len(path) >= 2:
                out.extend(path)
                i = j
                continue
        out.append(edges[i])
        i += 1
    return out


def _strip_colombo_moorhouse_uturn_hops(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """
    Replace -597576896#0 -> 597576896#0 (no on-street U-turn) with a vclass-feasible detour.

    Typical repair: approach TLS cluster, then loop via Moorhouse Ave and 807003661 south.
    """
    if not edges:
        return []
    out: list[str] = []
    i = 0
    while i < len(edges):
        if (
            i + 1 < len(edges)
            and edges[i] == COLOMBO_MOORHOUSE_UTURN_FROM
            and edges[i + 1] == COLOMBO_MOORHOUSE_UTURN_TO
        ):
            prev = out[-1] if out else None
            after = edges[i + 2] if i + 2 < len(edges) else None
            if prev and after:
                path = shortest_vclass_edge_path(net, prev, after, vclass)
                if not path or len(path) < 2:
                    return []
                out.extend(path[1:])
            elif prev:
                path = shortest_vclass_edge_path(
                    net, prev, COLOMBO_MOORHOUSE_UTURN_TO, vclass
                )
                if not path or len(path) < 2:
                    return []
                out.extend(path[1:])
            else:
                return []
            i += 2
            if i < len(edges) and out and edges[i] == out[-1]:
                i += 1
            continue
        out.append(edges[i])
        i += 1
    return out


def _strip_bus_forbidden_edge_hops(
    net,
    edges: list[str],
    vclass: str,
) -> list[str]:
    """Replace hops on BUS_FORBIDDEN_EDGES with bus-feasible detours."""
    if vclass != "bus" or not edges:
        return edges
    if not any(e in BUS_FORBIDDEN_EDGES for e in edges):
        return edges
    allow_uturn = vclass_allow_uturn(vclass)
    out: list[str] = []
    i = 0
    while i < len(edges):
        if edges[i] not in BUS_FORBIDDEN_EDGES:
            out.append(edges[i])
            i += 1
            continue
        prev = out[-1] if out else None
        j = i
        while j < len(edges) and edges[j] in BUS_FORBIDDEN_EDGES:
            j += 1
        if prev and j < len(edges):
            anchors = [prev]
            if out and out[-1] == "1015756824#1" and len(out) >= 2:
                anchors.insert(0, out[-2])
            path: list[str] | None = None
            target_idx = j
            for anchor in anchors:
                for k in range(j, min(j + 8, len(edges))):
                    candidate = shortest_vclass_edge_path_avoiding(
                        net,
                        anchor,
                        edges[k],
                        BUS_FORBIDDEN_EDGES,
                        vclass,
                        allow_uturn=allow_uturn,
                    )
                    if candidate and len(candidate) >= 2:
                        path = candidate
                        target_idx = k
                        if anchor != prev and out and out[-1] == "1015756824#1":
                            out.pop()
                        break
                if path:
                    break
            if path:
                out.extend(path[1:])
                i = target_idx
                continue
            return []
        i = j
    return out


def _repair_route_edge_list(
    net,
    edges: list[str],
    vclass: str = "passenger",
) -> list[str]:
    """Insert vclass-feasible hops between consecutive route edges."""
    if not edges:
        return []
    edges = _strip_false_opposite_busway_backward_hops(net, edges, vclass)
    if not edges:
        return []
    if vclass == "bus":
        edges = _redirect_saint_asaph_straight_to_bus_lane(net, edges, vclass)
        if not edges:
            return []
        edges = _repair_cbd_bus_corridors(net, edges, vclass)
        if not edges:
            return []
        edges = _repair_tuam_bus_lane_forbidden_exits(net, edges, vclass)
        if not edges:
            return []
        edges = _strip_bus_forbidden_edge_hops(net, edges, vclass)
        if not edges:
            return []
        return repair_bus_block_turns(net, edges, vclass)
    edges = _strip_colombo_moorhouse_uturn_hops(net, edges, vclass)
    if not edges:
        return []
    out = [edges[0]]
    for target in edges[1:]:
        if _lane_pair_connected_vclass(net, out[-1], target, vclass):
            out.append(target)
            continue
        path = shortest_vclass_edge_path(net, out[-1], target, vclass)
        if not path or len(path) < 2:
            return []
        out.extend(path[1:])
    return out


def shortest_edge_path(
    net, from_edge: str, to_edge: str, max_seen: int = 8000, max_hops: int = 120
) -> list[str]:
    """Shortest edge path (BFS) between two edges, including both ends."""
    if not from_edge or not to_edge:
        return []
    if from_edge == to_edge:
        return [from_edge]
    from collections import deque

    q: deque[tuple[str, list[str]]] = deque([(from_edge, [from_edge])])
    seen = {from_edge}
    while q:
        eid, path = q.popleft()
        if eid == to_edge:
            return path
        if len(path) >= max_hops:
            continue
        if len(seen) >= max_seen:
            continue
        for nid in _outgoing_edge_ids(net, eid):
            if nid not in seen:
                seen.add(nid)
                q.append((nid, path + [nid]))
    return []


def edge_reachable(net, from_edge: str, to_edge: str, max_seen: int = 8000) -> bool:
    """Whether a route exists between two edges (for alternate end selection)."""
    return bool(shortest_edge_path(net, from_edge, to_edge, max_seen=max_seen))


def _extend_chain_to_edge(net, chain: list[str], target: str) -> bool:
    """Append target to chain using a connected path; return False if unreachable."""
    if not target:
        return True
    if chain[-1] == target:
        return True
    if edge_connected(net, chain[-1], target):
        chain.append(target)
        return True
    path = shortest_edge_path(net, chain[-1], target)
    if not path or len(path) < 2:
        return False
    for eid in path[1:]:
        chain.append(eid)
    return True


_STREET_SUFFIXES = (
    " avenue",
    " ave",
    " street",
    " st",
    " road",
    " rd",
    " terrace",
    " tce",
    " place",
    " boulevard",
    " bvd",
    " drive",
    " dr",
    " square",
    " sq",
)


def normalize_street_name(label: str) -> str:
    text = (label or "").strip().lower()
    if not text:
        return ""
    for suffix in _STREET_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    return text


def approach_street_label(row: dict[str, str]) -> str:
    """Street name from survey headers (e.g. Harper Ave), when present."""
    for key in ("approach_header_1", "approach_header_2"):
        label = (row.get(key) or "").strip()
        if not label:
            continue
        lower = label.lower()
        if any(suf.strip() in lower for suf in _STREET_SUFFIXES):
            return label
        if any(
            lower.endswith(suf)
            for suf in (" ave", " st", " rd", " tce", " dr", " sq", " bvd")
        ):
            return label
    return ""


def approach_tokens(
    approach_bound: str,
    approach_header: str,
    approach_header_2: str = "",
) -> set[str]:
    text = f"{approach_bound} {approach_header} {approach_header_2}".lower()
    tokens: set[str] = set()
    for label in (
        "northbound",
        "southbound",
        "eastbound",
        "westbound",
        "northwestbound",
        "northeastbound",
        "southwestbound",
        "southeastbound",
    ):
        if label in text:
            tokens.add(label)
    if "northwest" in text:
        tokens.update({"northwestbound", "northbound", "westbound"})
    if "northeast" in text:
        tokens.update({"northeastbound", "northbound", "eastbound"})
    if "southwest" in text:
        tokens.update({"southwestbound", "southbound", "westbound"})
    if "southeast" in text:
        tokens.update({"southeastbound", "southbound", "eastbound"})
    return tokens


def _approach_candidate_edges(
    net,
    intersection_id: str,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    coord_map: dict[str, tuple[float, float]] | None = None,
) -> list:
    """Drivable edges that can host an approach movement at this intersection."""
    seen: set[str] = set()
    candidates: list = []

    def add(edge) -> None:
        eid = edge.getID()
        if eid in seen:
            return
        if not is_drivable_normal(edge):
            return
        if edge.getFromNode().getType() == "dead_end":
            return
        seen.add(eid)
        candidates.append(edge)

    junction_id = junction_map.get(intersection_id, "")
    if junction_id:
        try:
            node = net.getNode(junction_id)
        except Exception:
            node = None
        if node is not None:
            for edge in node.getIncoming():
                add(edge)

    exit_edge_id = exit_map.get(intersection_id, "")
    if exit_edge_id:
        try:
            exit_edge = net.getEdge(exit_edge_id)
        except Exception:
            exit_edge = None
        if exit_edge is not None:
            add(exit_edge)
            try:
                down = exit_edge.getToNode()
            except Exception:
                down = None
            if down is not None:
                for edge in down.getIncoming():
                    add(edge)

    coords = (coord_map or {}).get(intersection_id)
    if coords is not None:
        x, y = coords
        for lane, _dist in net.getNeighboringLanes(x, y, SEARCH_RADIUS_M):
            add(lane.getEdge())
    return candidates


def resolve_approach_enter_edge(
    net,
    intersection_id: str,
    row: dict[str, str],
    enter_map: dict[str, str],
    junction_map: dict[str, str],
    exit_map: dict[str, str] | None = None,
    coord_map: dict[str, tuple[float, float]] | None = None,
) -> str:
    """Incoming edge at the surveyed intersection for this movement approach."""
    exit_map = exit_map or {}
    fallback = enter_map.get(intersection_id, "")
    exit_edge_id = exit_map.get(intersection_id, "")
    candidates = _approach_candidate_edges(
        net,
        intersection_id,
        enter_map,
        exit_map,
        junction_map,
        coord_map=coord_map,
    )
    if not candidates:
        return fallback
    tokens = approach_tokens(
        row.get("approach_bound", ""),
        row.get("approach_header_1", ""),
        row.get("approach_header_2", ""),
    )
    street_want = normalize_street_name(approach_street_label(row))
    if not tokens and not street_want:
        return fallback
    scored: list[tuple[int, float, str]] = []
    for edge in candidates:
        compass = edge_compass_label(edge)
        score = 0
        if tokens:
            if compass in tokens:
                score += 3
            for token in tokens:
                if token.startswith(compass[:5]) or compass.startswith(token[:5]):
                    score += 1
        if street_want:
            edge_street = normalize_street_name(edge.getName() or "")
            if edge_street and (
                street_want == edge_street
                or street_want in edge_street
                or edge_street in street_want
            ):
                score += 6
        eid = edge.getID()
        if eid == fallback:
            score += 1
        if eid == exit_edge_id and compass in tokens:
            score += 2
        scored.append((score, edge.getLength(), eid))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2]
    return fallback


def _pick_aligned_boundary_edge(
    net,
    start_edge_id: str,
    candidates: list[tuple[str, int, str]],
    *,
    toward_sink: bool,
) -> str:
    """Pick a boundary trip end edge aligned with start_edge travel direction."""
    if not candidates:
        return ""
    start_edge = net.getEdge(start_edge_id)
    dir_x, dir_y = _edge_unit_vector(start_edge)
    shape = start_edge.getShape()
    if toward_sink:
        sx, sy = shape[-1]
    else:
        sx, sy = shape[0]
    best_eid = ""
    best_score = -999.0
    for eid, depth, junc_id in candidates:
        jx, jy = net.getNode(junc_id).getCoord()
        vx, vy = jx - sx, jy - sy
        mag = (vx * vx + vy * vy) ** 0.5
        if mag < 1e-6:
            align = 0.0
        else:
            align = (dir_x * vx / mag) + (dir_y * vy / mag)
        score = align - (0.05 * depth)
        if score > best_score:
            best_score = score
            best_eid = eid
    return best_eid


def walk_to_boundary_source(
    net,
    start_edge_id: str,
    dead_junctions: set[str],
    max_hops: int = 120,
) -> str:
    """Upstream from approach edge to network-boundary source (trip from= edge)."""
    from collections import deque

    if not start_edge_id:
        return ""
    boundary = BOUNDARY_DEAD_END_JUNCTION_IDS
    q: deque[tuple[str, int]] = deque([(start_edge_id, 0)])
    seen = {start_edge_id}
    boundary_hits: list[tuple[str, int, str]] = []
    fallback = ""
    while q:
        eid, depth = q.popleft()
        edge = net.getEdge(eid)
        from_junc = edge.getFromNode().getID()
        if from_junc in boundary:
            boundary_hits.append((eid, depth, from_junc))
        elif from_junc in dead_junctions and not fallback:
            fallback = eid
        if depth >= max_hops:
            continue
        for inc in edge.getFromNode().getIncoming():
            if not is_drivable_normal(inc):
                continue
            nid = inc.getID()
            if nid not in seen:
                seen.add(nid)
                q.append((nid, depth + 1))
    if boundary_hits:
        return _pick_aligned_boundary_edge(
            net, start_edge_id, boundary_hits, toward_sink=False
        )
    return fallback


def walk_to_boundary_sink(
    net,
    start_edge_id: str,
    dead_junctions: set[str],
    max_hops: int = 120,
) -> str:
    """Downstream from exit edge to network-boundary sink (trip to= edge)."""
    from collections import deque

    if not start_edge_id:
        return ""
    boundary = BOUNDARY_DEAD_END_JUNCTION_IDS
    q: deque[tuple[str, int]] = deque([(start_edge_id, 0)])
    seen = {start_edge_id}
    boundary_hits: list[tuple[str, int, str]] = []
    fallback = ""
    while q:
        eid, depth = q.popleft()
        edge = net.getEdge(eid)
        to_junc = edge.getToNode().getID()
        if to_junc in boundary:
            boundary_hits.append((eid, depth, to_junc))
        elif to_junc in dead_junctions and not fallback:
            fallback = eid
        if depth >= max_hops:
            continue
        for out in edge.getToNode().getOutgoing():
            if not is_drivable_normal(out):
                continue
            nid = out.getID()
            if nid not in seen:
                seen.add(nid)
                q.append((nid, depth + 1))
    if boundary_hits:
        return _pick_aligned_boundary_edge(
            net, start_edge_id, boundary_hits, toward_sink=True
        )
    return fallback


def walk_to_boundary_source_vclass(
    net,
    start_edge_id: str,
    dead_junctions: set[str],
    vclass: str = "bus",
    max_hops: int = 120,
) -> str:
    """Upstream to a boundary dead-end source edge (vclass-feasible)."""
    from collections import deque

    if not start_edge_id:
        return ""
    if is_boundary_source_edge(net, start_edge_id):
        return start_edge_id
    boundary = BOUNDARY_DEAD_END_JUNCTION_IDS
    allow_uturn = vclass_allow_uturn(vclass)
    q: deque[tuple[str, int]] = deque([(start_edge_id, 0)])
    seen = {start_edge_id}
    boundary_hits: list[tuple[str, int, str]] = []
    fallback = ""
    while q:
        eid, depth = q.popleft()
        edge = net.getEdge(eid)
        from_junc = edge.getFromNode().getID()
        if from_junc in boundary:
            boundary_hits.append((eid, depth, from_junc))
        elif from_junc in dead_junctions and not fallback and is_drivable_vclass(
            edge, vclass
        ):
            fallback = eid
        if depth >= max_hops:
            continue
        for inc in edge.getFromNode().getIncoming():
            if inc.getFunction() or (inc.getID() or "").startswith(":"):
                continue
            if not is_drivable_vclass(inc, vclass):
                continue
            nid = inc.getID()
            if not edge_connected_vclass(
                net, nid, eid, vclass, allow_uturn=allow_uturn
            ):
                continue
            if nid not in seen:
                seen.add(nid)
                q.append((nid, depth + 1))
    if boundary_hits:
        return _pick_aligned_boundary_edge(
            net, start_edge_id, boundary_hits, toward_sink=False
        )
    return fallback


def walk_to_boundary_sink_vclass(
    net,
    start_edge_id: str,
    dead_junctions: set[str],
    vclass: str = "bus",
    max_hops: int = 120,
) -> str:
    """Downstream to a boundary dead-end sink edge (vclass-feasible)."""
    from collections import deque

    if not start_edge_id:
        return ""
    if is_boundary_sink_edge(net, start_edge_id):
        return start_edge_id
    boundary = BOUNDARY_DEAD_END_JUNCTION_IDS
    allow_uturn = vclass_allow_uturn(vclass)
    q: deque[tuple[str, int]] = deque([(start_edge_id, 0)])
    seen = {start_edge_id}
    boundary_hits: list[tuple[str, int, str]] = []
    fallback = ""
    while q:
        eid, depth = q.popleft()
        edge = net.getEdge(eid)
        to_junc = edge.getToNode().getID()
        if to_junc in boundary:
            boundary_hits.append((eid, depth, to_junc))
        elif to_junc in dead_junctions and not fallback and is_drivable_vclass(
            edge, vclass
        ):
            fallback = eid
        if depth >= max_hops:
            continue
        for out in edge.getOutgoing():
            if out.getFunction() or (out.getID() or "").startswith(":"):
                continue
            if not is_drivable_vclass(out, vclass):
                continue
            nid = out.getID()
            if not edge_connected_vclass(
                net, eid, nid, vclass, allow_uturn=allow_uturn
            ):
                continue
            if nid not in seen:
                seen.add(nid)
                q.append((nid, depth + 1))
    if boundary_hits:
        return _pick_aligned_boundary_edge(
            net, start_edge_id, boundary_hits, toward_sink=True
        )
    return fallback


def dead_end_junction_ids(net) -> set[str]:
    """All dead_end junctions plus promoted network-boundary stubs."""
    return {
        n.getID() for n in net.getNodes() if n.getType() == "dead_end"
    } | BOUNDARY_DEAD_END_JUNCTION_IDS


def trim_selwyn_boundary_stubs(
    net,
    edges: list[str],
    vclass: str = "bus",
) -> list[str]:
    """
    Drop Selwyn St clip stubs; keep buses on interior TLS edges instead.

    The promoted dead_end at 8871638711 misaligns with 1015757970 lane shapes, so
    buses sourced on -1015757970#1 or sinked on 1015757970#1 appear underground.
    """
    if vclass != "bus" or len(edges) < 2:
        return edges
    out = list(edges)
    if out[0] == SELWYN_BOUNDARY_SOURCE_EDGE:
        out = out[1:]
    if out and out[-1] == SELWYN_BOUNDARY_SINK_EDGE:
        out = out[:-1]
    if out == edges:
        return edges
    if len(out) < 2 or not _route_edges_connected(net, out, vclass):
        return edges
    return out


def extend_route_to_boundary_dead_ends(
    net,
    edges: list[str],
    vclass: str = "bus",
) -> list[str]:
    """
    Extend a clipped in-network route to boundary dead-end source/sink edges.

    Uses travel direction along the route (first/last core edges) and the same
    boundary stubs as car demand (BOUNDARY_DEAD_END_JUNCTION_IDS).
    """
    if len(edges) < 2:
        return edges
    dead = dead_end_junction_ids(net)
    core = list(edges)
    core_start, core_end = core[0], core[-1]
    source = walk_to_boundary_source_vclass(net, core_start, dead, vclass)
    sink = walk_to_boundary_sink_vclass(net, core_end, dead, vclass)
    sink_anchor = core_end
    if not sink and TUAM_BUS_LANE_JUNCTION_EDGE in core:
        sink_anchor = TUAM_BUS_LANE_JUNCTION_EDGE
        sink = walk_to_boundary_sink_vclass(net, sink_anchor, dead, vclass)
        if sink:
            idx = core.index(sink_anchor)
            core = core[: idx + 1]
            core_end = core[-1]
    if not sink:
        for anchor in reversed(core[-6:]):
            if anchor == core_end:
                continue
            candidate = walk_to_boundary_sink_vclass(net, anchor, dead, vclass)
            if candidate:
                sink = candidate
                sink_anchor = anchor
                idx = core.index(anchor)
                core = core[: idx + 1]
                core_end = core[-1]
                break
    if not source and not sink:
        return edges

    from_edge = source or core_start
    to_edge = sink or core_end
    if from_edge == to_edge:
        return edges

    chain = [from_edge]
    if not _extend_chain_vclass(net, chain, core_start, vclass):
        return edges
    if chain[-1] == core_start:
        chain.extend(core[1:])
    else:
        try:
            idx = core.index(chain[-1])
            chain.extend(core[idx + 1 :])
        except ValueError:
            return edges
    if chain[-1] != to_edge and not _extend_chain_vclass(net, chain, to_edge, vclass):
        return edges
    if len(chain) < 2 or chain[0] != from_edge or chain[-1] != to_edge:
        return edges
    if vclass == "bus":
        repaired = repair_bus_block_turns(net, chain, vclass)
        if not repaired:
            return edges
        chain = repaired
    if not _route_edges_connected(net, chain, vclass):
        return edges
    if not is_boundary_source_edge(net, chain[0]) or not is_boundary_sink_edge(
        net, chain[-1]
    ):
        return edges
    return chain


def stub_boundary_trip_edges(net, junction_id: str) -> tuple[str, str]:
    """Depart/ arrive edges at a boundary dead_end (out of / into the network)."""
    try:
        node = net.getNode(junction_id)
    except Exception:
        return "", ""
    if node.getType() != "dead_end":
        return "", ""
    source = ""
    sink = ""
    for edge in node.getOutgoing():
        if is_drivable_normal(edge):
            source = edge.getID()
            break
    for edge in node.getIncoming():
        if is_drivable_normal(edge):
            sink = edge.getID()
            break
    return source, sink


def build_boundary_edge_maps(
    net_path: Path,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str] | None = None,
    *,
    on_tick: Callable[[int, int], None] | None = None,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    set[str],
    set[str],
    object,
]:
    """Map approach/exit edges and intersections to boundary dead-end trip ends."""
    setup_sumolib()
    import sumolib.net as sumo_net  # noqa: E402

    def tick(index: int, total: int) -> None:
        if on_tick is not None:
            on_tick(index, total)

    enters = sorted({e for e in enter_map.values() if e})
    exits = sorted({e for e in exit_map.values() if e})
    iids = sorted(set(enter_map) | set(exit_map))
    total_work = 1 + len(enters) + len(exits) + len(iids)
    work_i = 0

    tick(work_i, total_work)
    net = sumo_net.readNet(str(net_path), withInternal=False)
    work_i += 1
    tick(work_i, total_work)
    dead_junctions = {n.getID() for n in net.getNodes() if n.getType() == "dead_end"}
    dead_junctions |= BOUNDARY_DEAD_END_JUNCTION_IDS

    from_map: dict[str, str] = {}
    to_map: dict[str, str] = {}
    source_by_enter: dict[str, str] = {}
    sink_by_exit: dict[str, str] = {}
    source_edges: set[str] = set()
    sink_edges: set[str] = set()

    if junction_map:
        for iid, jid in junction_map.items():
            if jid not in BOUNDARY_DEAD_END_JUNCTION_IDS:
                continue
            src, sink = stub_boundary_trip_edges(net, jid)
            if src:
                from_map[iid] = src
                source_edges.add(src)
            if sink:
                to_map[iid] = sink
                sink_edges.add(sink)

    for jid in BOUNDARY_DEAD_END_JUNCTION_IDS:
        src, sink = stub_boundary_trip_edges(net, jid)
        if src:
            source_edges.add(src)
        if sink:
            sink_edges.add(sink)

    for enter in enters:
        if enter in source_by_enter:
            work_i += 1
            tick(work_i, total_work)
            continue
        src = walk_to_boundary_source(net, enter, dead_junctions)
        if src:
            source_by_enter[enter] = src
            source_edges.add(src)
        work_i += 1
        tick(work_i, total_work)

    for exit_ in exits:
        if exit_ in sink_by_exit:
            work_i += 1
            tick(work_i, total_work)
            continue
        sink = walk_to_boundary_sink(net, exit_, dead_junctions)
        if sink:
            sink_by_exit[exit_] = sink
            sink_edges.add(sink)
        work_i += 1
        tick(work_i, total_work)

    for iid in iids:
        enter = enter_map.get(iid, "")
        exit_ = exit_map.get(iid, "")
        if enter:
            from_map[iid] = source_by_enter.get(enter, "")
        if exit_:
            to_map[iid] = sink_by_exit.get(exit_, "")
        work_i += 1
        tick(work_i, total_work)

    return from_map, to_map, source_by_enter, sink_by_exit, source_edges, sink_edges, net


def is_turn_movement(movement: str) -> bool:
    """True for counted turns (not through movements)."""
    m = (movement or "").strip().lower()
    if m in ("left", "right"):
        return True
    if "bear left" in m or "bear right" in m:
        return True
    if "hard left" in m or "hard right" in m:
        return True
    return "u-turn" in m or m in ("uturn", "u turn")


def movement_connection_dir_groups(movement: str) -> list[list[str]]:
    """SUMO connection dir codes for a movement, in preference order.

    Survey bearings do not always match SUMO's left/right at a leg (e.g. a
    counted right turn may be connection dir ``l`` onto Worcester St).
    """
    m = (movement or "").strip().lower()
    if "u-turn" in m or m in ("uturn", "u turn"):
        return [["T", "t"]]
    if "hard left" in m or "bear left" in m or m == "left":
        return [["L", "l"], ["r", "R"]]
    if "hard right" in m or "bear right" in m or m == "right":
        return [["r", "R"], ["l", "L"]]
    if m in ("thru", "through"):
        return [["s", "S"]]
    return []


def movement_connection_dirs(movement: str) -> list[str]:
    """Flat SUMO connection dir codes for a traffic-count movement label."""
    dirs: list[str] = []
    for group in movement_connection_dir_groups(movement):
        for d in group:
            if d not in dirs:
                dirs.append(d)
    return dirs


def resolve_movement_turn_edge(
    net,
    approach_enter: str,
    movement: str,
) -> str:
    """Outgoing edge at approach_enter that matches the counted turn movement."""
    dir_groups = movement_connection_dir_groups(movement)
    if not approach_enter or not dir_groups:
        return ""
    try:
        edge = net.getEdge(approach_enter)
    except Exception:
        return ""
    for dirs in dir_groups:
        for out_edge in edge.getOutgoing():
            for conn in edge.getConnections(out_edge):
                if conn.getDirection() in dirs:
                    return out_edge.getID()
    return ""


def boundary_source_for_approach(
    net,
    approach_enter: str,
    source_by_enter: dict[str, str],
    origin_fallback: str,
    dead_junctions: set[str],
) -> str:
    """Boundary depart edge for the movement approach (not only default enter_edge)."""
    if approach_enter:
        cached = source_by_enter.get(approach_enter)
        if cached:
            return cached
        walked = walk_to_boundary_source(net, approach_enter, dead_junctions)
        if walked:
            source_by_enter[approach_enter] = walked
            return walked
    return origin_fallback


def boundary_sink_for_exit(
    net,
    dest_exit: str,
    sink_by_exit: dict[str, str],
    dest_fallback: str,
    dead_junctions: set[str],
) -> str:
    """Boundary arrival edge for the destination leg."""
    if dest_exit:
        cached = sink_by_exit.get(dest_exit)
        if cached:
            return cached
        walked = walk_to_boundary_sink(net, dest_exit, dead_junctions)
        if walked:
            sink_by_exit[dest_exit] = walked
            return walked
    return dest_fallback


def boundary_trip_edges(
    origin_id: str,
    dest_id: str,
    row: dict[str, str],
    net,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    source_by_enter: dict[str, str],
    sink_by_exit: dict[str, str],
    from_map: dict[str, str],
    to_map: dict[str, str],
    coord_map: dict[str, tuple[float, float]] | None = None,
    dead_junctions: set[str] | None = None,
) -> tuple[str, str, str, str]:
    """Return (from_boundary, to_boundary, approach_enter, dest_exit)."""
    approach_enter = resolve_approach_enter_edge(
        net,
        origin_id,
        row,
        enter_map,
        junction_map,
        exit_map,
        coord_map=coord_map,
    )
    dest_exit = exit_map.get(dest_id, "")
    origin_fallback = from_map.get(origin_id, "")
    dest_fallback = to_map.get(dest_id, "")
    dead = dead_junctions or set()
    from_edge = boundary_source_for_approach(
        net, approach_enter, source_by_enter, origin_fallback, dead
    )
    to_edge = boundary_sink_for_exit(
        net, dest_exit, sink_by_exit, dest_fallback, dead
    )
    return from_edge, to_edge, approach_enter, dest_exit


def walk_sink_after_turn(net, turn_edge: str, max_hops: int = 80) -> str:
    """Nearest downstream boundary-stub sink edge after a turn movement."""
    from collections import deque

    if not turn_edge:
        return ""
    boundary = BOUNDARY_DEAD_END_JUNCTION_IDS
    q: deque[tuple[str, int]] = deque([(turn_edge, 0)])
    seen = {turn_edge}
    best_edge = ""
    best_depth = max_hops + 1
    while q:
        eid, depth = q.popleft()
        to_junc = net.getEdge(eid).getToNode().getID()
        if to_junc in boundary and depth < best_depth:
            best_depth = depth
            best_edge = eid
        if depth >= max_hops:
            continue
        for nid in _outgoing_edge_ids(net, eid):
            if nid not in seen:
                seen.add(nid)
                q.append((nid, depth + 1))
    return best_edge


def resolve_turn_only_trip_edges(
    net,
    origin_id: str,
    row: dict[str, str],
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    source_by_enter: dict[str, str],
    from_map: dict[str, str],
    dead_junctions: set[str],
    coord_map: dict[str, tuple[float, float]] | None = None,
) -> tuple[str, str, str, str]:
    """Boundary OD for a turn with no destination_id: exit via the turn leg."""
    approach_enter = resolve_approach_enter_edge(
        net,
        origin_id,
        row,
        enter_map,
        junction_map,
        exit_map,
        coord_map=coord_map,
    )
    movement = (row.get("movement") or "").strip()
    turn_edge = resolve_movement_turn_edge(net, approach_enter, movement)
    if not approach_enter or not turn_edge:
        return "", "", "", ""
    from_edge = boundary_source_for_approach(
        net,
        approach_enter,
        source_by_enter,
        from_map.get(origin_id, ""),
        dead_junctions,
    )
    to_edge = walk_sink_after_turn(net, turn_edge)
    if not to_edge:
        to_edge = walk_to_boundary_sink(net, turn_edge, dead_junctions)
    return from_edge, to_edge, approach_enter, ""


def _trip_waypoint_edge(
    net,
    edge_id: str,
    from_edge: str,
    to_edge: str,
) -> bool:
    """Interior edge that should appear on a generated trip route."""
    if not edge_id or edge_id in (from_edge, to_edge):
        return False
    if is_boundary_source_edge(net, edge_id) or is_boundary_sink_edge(net, edge_id):
        return False
    return True


def _extend_chain_vclass(
    net, chain: list[str], target: str, vclass: str
) -> bool:
    """Append target using a vclass-feasible path (lane/bus restrictions respected)."""
    if not target:
        return True
    if chain[-1] == target:
        return True
    allow_uturn = vclass_allow_uturn(vclass)
    if edge_connected_vclass(
        net, chain[-1], target, vclass, allow_uturn=allow_uturn
    ):
        chain.append(target)
        return True
    path = shortest_vclass_edge_path(
        net, chain[-1], target, vclass, allow_uturn=allow_uturn
    )
    if not path or len(path) < 2:
        return False
    for eid in path[1:]:
        chain.append(eid)
    return True


def build_trip_route_edges(
    net,
    approach_enter: str,
    dest_exit: str,
    from_edge: str,
    to_edge: str,
    movement: str = "",
    *,
    vclass: str = "passenger",
) -> list[str]:
    """Full edge chain from boundary source to sink (approach + turn + optional dest exit)."""
    if not from_edge or not to_edge:
        return []

    turn_edge = resolve_movement_turn_edge(net, approach_enter, movement)
    waypoints: list[str] = []
    if _trip_waypoint_edge(net, approach_enter, from_edge, to_edge):
        waypoints.append(approach_enter)
    if turn_edge and _trip_waypoint_edge(net, turn_edge, from_edge, to_edge):
        waypoints.insert(
            1 if approach_enter in waypoints else 0,
            turn_edge,
        )
    if (
        dest_exit
        and _trip_waypoint_edge(net, dest_exit, from_edge, to_edge)
        and dest_exit not in waypoints
        and (
            not turn_edge
            or bool(
                shortest_vclass_edge_path(
                    net,
                    turn_edge,
                    dest_exit,
                    vclass,
                    allow_uturn=vclass_allow_uturn(vclass),
                )
            )
        )
    ):
        waypoints.append(dest_exit)

    chain = [from_edge]
    for waypoint in waypoints:
        prev_len = len(chain)
        if not _extend_chain_vclass(net, chain, waypoint, vclass):
            if waypoint == turn_edge:
                return []
            continue
        if waypoint == turn_edge:
            continue
        if not shortest_vclass_edge_path(
            net,
            chain[-1],
            to_edge,
            vclass,
            allow_uturn=vclass_allow_uturn(vclass),
        ):
            del chain[prev_len:]

    if not _extend_chain_vclass(net, chain, to_edge, vclass):
        direct = shortest_vclass_edge_path(
            net,
            from_edge,
            to_edge,
            vclass,
            allow_uturn=vclass_allow_uturn(vclass),
        )
        if not direct:
            return []
        chain = direct

    if len(chain) < 2 or chain[0] != from_edge or chain[-1] != to_edge:
        return []
    if vclass == "bus":
        return repair_bus_block_turns(net, chain, vclass)
    return chain


def build_trip_via(
    net,
    approach_enter: str,
    dest_exit: str,
    from_edge: str,
    to_edge: str,
    movement: str = "",
    *,
    vclass: str = "passenger",
) -> str:
    """Intermediate edges between from= and to= (legacy via attribute)."""
    chain = build_trip_route_edges(
        net,
        approach_enter,
        dest_exit,
        from_edge,
        to_edge,
        movement=movement,
        vclass=vclass,
    )
    if len(chain) <= 2:
        return ""
    return " ".join(chain[1:-1])


_TIME_BEGIN_RE = re.compile(r"(\d{1,2}):(\d{2})")


def time_to_begin_sec(time_str: str) -> int:
    m = _TIME_BEGIN_RE.match((time_str or "").strip())
    if not m:
        return 0
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 3600 + mi * 60


def safe_id(*parts: str) -> str:
    raw = "_".join(parts)
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:120]


def motor_vehicle_counts(row: dict[str, str]) -> tuple[int, int]:
    """Return (cars from lights sheet, buses/other from other_vehicles sheet)."""
    return int(row.get("lights") or 0), int(row.get("other_vehicles") or 0)


def motor_vehicle_total(row: dict[str, str]) -> int:
    cars, buses = motor_vehicle_counts(row)
    return cars + buses


def cap_row_motor_counts(
    car_count: int, bus_count: int, row: dict[str, str]
) -> tuple[int, int]:
    """Limit cars/buses so they do not exceed the survey ``totals`` cell."""
    totals = int(row.get("totals") or 0)
    if totals <= 0:
        return car_count, bus_count
    car_out = min(car_count, totals)
    bus_out = min(bus_count, max(0, totals - car_out))
    return car_out, bus_out


def _period_interval_key(
    rt: ResolvedTripRow,
) -> tuple[str, str, str] | None:
    """Survey period (AM/PM) plus count interval time, e.g. 06:30."""
    period = (rt.row.get("period") or "").strip()
    time_str = (rt.row.get("time") or "").strip()
    if not period or not time_str:
        return None
    return period, time_str


def _sum_period_vehicle_budgets(
    resolved: list[ResolvedTripRow],
) -> tuple[
    dict[tuple[str, str, str, str], int],
    dict[tuple[str, str, str, str], int],
    dict[tuple[str, str, str], int],
]:
    """
    Budgets per vehicle type for intersection+period+interval and boundary source.

    Also a combined ``totals`` pool per (source, period, interval) from the CSV.
    """
    by_intersection: dict[tuple[str, str, str, str], int] = {}
    by_source: dict[tuple[str, str, str, str], int] = {}
    source_totals_pool: dict[tuple[str, str, str], int] = {}
    for rt in resolved:
        interval = _period_interval_key(rt)
        if interval is None:
            continue
        period, time_str = interval
        if rt.car_count > 0 and rt.origin_id:
            k = (rt.origin_id, period, time_str, "car")
            by_intersection[k] = by_intersection.get(k, 0) + rt.car_count
            if rt.demand_from:
                ks = (rt.demand_from, period, time_str, "car")
                by_source[ks] = by_source.get(ks, 0) + rt.car_count
        if rt.bus_count > 0 and rt.origin_id:
            k = (rt.origin_id, period, time_str, "bus")
            by_intersection[k] = by_intersection.get(k, 0) + rt.bus_count
            if rt.demand_from:
                ks = (rt.demand_from, period, time_str, "bus")
                by_source[ks] = by_source.get(ks, 0) + rt.bus_count
        if rt.demand_from:
            totals = int(rt.row.get("totals") or 0)
            if totals > 0:
                kp = (rt.demand_from, period, time_str)
                source_totals_pool[kp] = (
                    source_totals_pool.get(kp, 0) + totals
                )
    return by_intersection, by_source, source_totals_pool


def _take_period_budget(
    count: int,
    key: tuple[str, str, str, str],
    budget: dict[tuple[str, str, str, str], int],
    used: dict[tuple[str, str, str, str], int],
) -> int:
    if count <= 0:
        return 0
    remaining = budget.get(key, 0) - used.get(key, 0)
    out = min(count, max(0, remaining))
    if out > 0:
        used[key] = used.get(key, 0) + out
    return out


def _take_source_totals_pool(
    car: int,
    bus: int,
    pool_key: tuple[str, str, str],
    pool_budget: dict[tuple[str, str, str], int],
    pool_used: dict[tuple[str, str, str], int],
) -> tuple[int, int]:
    """Cars then buses from the survey ``totals`` pool at this source+interval."""
    remaining = pool_budget.get(pool_key, 0) - pool_used.get(pool_key, 0)
    if remaining <= 0:
        return 0, 0
    car_out = min(car, remaining)
    remaining -= car_out
    bus_out = min(bus, remaining)
    used = pool_used.get(pool_key, 0) + car_out + bus_out
    if used > 0:
        pool_used[pool_key] = used
    return car_out, bus_out


def apply_period_vehicle_caps(
    resolved: list[ResolvedTripRow],
) -> tuple[int, int, int, int]:
    """
    Trim vehicle counts in CSV order so sourcing stops at survey totals.

    Returns (trimmed by row totals, intersection+interval, source+interval,
    source totals pool).
    """
    ix_budget, src_budget, src_pool = _sum_period_vehicle_budgets(resolved)
    ix_used: dict[tuple[str, str, str, str], int] = {}
    src_used: dict[tuple[str, str, str, str], int] = {}
    pool_used: dict[tuple[str, str, str], int] = {}
    trimmed_totals = trimmed_ix = trimmed_src = trimmed_pool = 0

    for rt in resolved:
        before = rt.car_count + rt.bus_count
        car, bus = cap_row_motor_counts(rt.car_count, rt.bus_count, rt.row)
        trimmed_totals += before - (car + bus)

        interval = _period_interval_key(rt)
        if interval is not None and rt.origin_id:
            period, time_str = interval
            car_before, bus_before = car, bus
            car = _take_period_budget(
                car, (rt.origin_id, period, time_str, "car"), ix_budget, ix_used
            )
            bus = _take_period_budget(
                bus, (rt.origin_id, period, time_str, "bus"), ix_budget, ix_used
            )
            trimmed_ix += (car_before + bus_before) - (car + bus)

        if interval is not None and rt.demand_from:
            period, time_str = interval
            car_before, bus_before = car, bus
            car = _take_period_budget(
                car,
                (rt.demand_from, period, time_str, "car"),
                src_budget,
                src_used,
            )
            bus = _take_period_budget(
                bus,
                (rt.demand_from, period, time_str, "bus"),
                src_budget,
                src_used,
            )
            trimmed_src += (car_before + bus_before) - (car + bus)
            pool_key = (rt.demand_from, period, time_str)
            car_before, bus_before = car, bus
            car, bus = _take_source_totals_pool(
                car, bus, pool_key, src_pool, pool_used
            )
            trimmed_pool += (car_before + bus_before) - (car + bus)

        rt.car_count = car
        rt.bus_count = bus

    return trimmed_totals, trimmed_ix, trimmed_src, trimmed_pool


def _movement_key(row: dict[str, str]) -> tuple[str, str, str]:
    origin_id = (row.get("intersection_id") or "").strip()
    approach_bound = (row.get("approach_bound") or "").strip()
    movement = (row.get("movement") or "").strip()
    return origin_id, approach_bound, movement


def read_traffic_rows(traffic_csv: Path) -> list[dict[str, str]]:
    with traffic_csv.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_dest_inference(
    traffic_rows: list[dict[str, str]],
    intersections: frozenset[str] | None = None,
) -> tuple[dict[tuple[str, str, str], str], dict[tuple[str, str], str], dict[str, str]]:
    """Most common destination_id for rows that have one (by approach/movement, then fallbacks)."""
    from collections import Counter, defaultdict

    by_key: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    by_mov: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    by_origin: dict[str, Counter[str]] = defaultdict(Counter)

    for row in traffic_rows:
        count = motor_vehicle_total(row)
        if count <= 0:
            continue
        origin_id, approach_bound, movement = _movement_key(row)
        dest_id = (row.get("destination_id") or "").strip()
        if not origin_id or not dest_id:
            continue
        if intersections is not None:
            if origin_id not in intersections or dest_id not in intersections:
                continue
        by_key[(origin_id, approach_bound, movement)][dest_id] += count
        by_mov[(origin_id, movement)][dest_id] += count
        by_origin[origin_id][dest_id] += count

    def _pick(counter: Counter[str]) -> str:
        return counter.most_common(1)[0][0] if counter else ""

    key3 = {k: _pick(c) for k, c in by_key.items()}
    key2 = {k: _pick(c) for k, c in by_mov.items()}
    key1 = {k: _pick(c) for k, c in by_origin.items()}
    return key3, key2, key1


def resolve_destination_id(
    row: dict[str, str],
    dest_key3: dict[tuple[str, str, str], str],
    dest_key2: dict[tuple[str, str], str],
    dest_key1: dict[str, str],
) -> tuple[str, bool]:
    """Return (destination_id, inferred). Empty if unknown."""
    dest_id = (row.get("destination_id") or "").strip()
    if dest_id:
        return dest_id, False
    origin_id, approach_bound, movement = _movement_key(row)
    if not origin_id:
        return "", False
    if (origin_id, approach_bound, movement) in dest_key3:
        return dest_key3[(origin_id, approach_bound, movement)], True
    if is_turn_movement(movement):
        return "", False
    if (origin_id, movement) in dest_key2:
        return dest_key2[(origin_id, movement)], True
    if origin_id in dest_key1:
        return dest_key1[origin_id], True
    return "", False


@dataclass(slots=True)
class ResolvedTripRow:
    """One traffic row after a single boundary/route resolution pass."""

    row: dict[str, str]
    car_count: int
    bus_count: int
    origin_id: str
    dest_id: str
    inferred: bool
    movement: str
    begin: int
    approach_enter: str
    dest_exit: str
    demand_from: str
    demand_to: str
    from_edge: str
    to_edge: str


def _cbd_centroid_net(net) -> tuple[float, float]:
    """Mean position of cordon dead-end junctions (network projection coords)."""
    xs: list[float] = []
    ys: list[float] = []
    for jid in BOUNDARY_DEAD_END_JUNCTION_IDS:
        try:
            x, y = net.getNode(jid).getCoord()
        except Exception:
            continue
        xs.append(x)
        ys.append(y)
    if not xs:
        return (0.0, 0.0)
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _boundary_edge_cordon_sector(
    net,
    edge_id: str,
    centroid: tuple[float, float],
) -> str:
    """Cardinal cordon sector (south/north/east/west) for a boundary trip end edge."""
    if not edge_id:
        return ""
    try:
        edge = net.getEdge(edge_id)
    except Exception:
        return ""
    if is_boundary_source_edge(net, edge_id):
        jx, jy = edge.getFromNode().getCoord()
    elif is_boundary_sink_edge(net, edge_id):
        jx, jy = edge.getToNode().getCoord()
    else:
        return ""
    cx, cy = centroid
    dx, dy = jx - cx, jy - cy
    if abs(dy) >= abs(dx):
        return "south" if dy < 0 else "north"
    return "west" if dx < 0 else "east"


def _scale_counts_to_target(counts: list[int], target: int) -> list[int]:
    """Scale non-negative integers to sum to target (largest-remainder rounding)."""
    total = sum(counts)
    if total <= 0 or target <= 0:
        return [0] * len(counts)
    if total == target:
        return list(counts)
    scaled = [c * target / total for c in counts]
    out = [int(v) for v in scaled]
    remainder = target - sum(out)
    if remainder > 0:
        order = sorted(
            range(len(counts)),
            key=lambda i: scaled[i] - out[i],
            reverse=True,
        )
        for i in order[:remainder]:
            out[i] += 1
    return out


def _share_targets(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Split total across share keys (integer, sums to total)."""
    if total <= 0:
        return {k: 0 for k in shares}
    scaled = {k: total * shares[k] for k in shares}
    out = {k: int(scaled[k]) for k in shares}
    remainder = total - sum(out.values())
    if remainder > 0:
        order = sorted(
            shares,
            key=lambda k: scaled[k] - out[k],
            reverse=True,
        )
        for k in order[:remainder]:
            out[k] += 1
    return out


def _survey_window_seconds(resolved: list[ResolvedTripRow]) -> int:
    """Duration of unique 15-minute count intervals present in resolved demand."""
    begins = {rt.begin for rt in resolved if rt.car_count > 0 or rt.bus_count > 0}
    return max(1, len(begins) * INTERVAL_SEC)


def _hour_from_begin(begin: int) -> int:
    return (begin // 3600) % 24


def _hourly_profile_weight(hour: int, hourly_profile: tuple[int, ...]) -> float:
    return float(hourly_profile[hour % len(hourly_profile)])


def _interval_targets_by_hourly_profile(
    begins: set[int],
    hourly_profile: tuple[int, ...],
    window_total: int,
) -> dict[int, int]:
    """Split window_total across 15-min intervals proportional to hourly profile."""
    if window_total <= 0 or not begins:
        return {b: 0 for b in begins}
    weights = {
        b: _hourly_profile_weight(_hour_from_begin(b), hourly_profile) for b in begins
    }
    wsum = sum(weights.values())
    if wsum <= 0:
        per = window_total // len(begins)
        return {b: per for b in begins}
    return {
        b: int(round(window_total * weights[b] / wsum)) for b in begins
    }


def _interval_cap_from_hourly_profile(
    begin: int,
    hourly_profile: tuple[int, ...],
    ceiling_per_hour: int | None = None,
) -> int:
    """Max vehicles in a 15-min interval from hourly rate (optional hard ceiling)."""
    rate = _hourly_profile_weight(_hour_from_begin(begin), hourly_profile)
    if ceiling_per_hour is not None:
        rate = min(rate, float(ceiling_per_hour))
    return max(1, int(round(rate * INTERVAL_SEC / 3600)))


def apply_smartview_cordon_calibration(
    resolved: list[ResolvedTripRow],
    net,
    *,
    daily_cars: int = CCC_SMARTVIEW_DAILY_CORDON_CARS,
    direction_share: dict[str, float] | None = None,
    hourly_profile: tuple[int, ...] | None = None,
) -> tuple[int, float]:
    """
    Scale boundary source car counts to CCC SmartView cordon volume and direction mix.

    Targets daily cordon entries (cars), M-curve hourly split, and per-interval
    South/North/East/West shares. Returns (vehicles trimmed, global scale factor).
    """
    profile = hourly_profile if hourly_profile is not None else CCC_CORDON_HOURLY_VPH
    shares = direction_share or CCC_CORDON_DIRECTION_SHARE
    centroid = _cbd_centroid_net(net)
    sector_cache: dict[str, str] = {}

    def sector(edge_id: str) -> str:
        if edge_id not in sector_cache:
            sector_cache[edge_id] = _boundary_edge_cordon_sector(net, edge_id, centroid)
        return sector_cache[edge_id]

    cordon_idxs = [
        i
        for i, rt in enumerate(resolved)
        if rt.car_count > 0 and is_boundary_source_edge(net, rt.demand_from)
    ]
    raw_total = sum(resolved[i].car_count for i in cordon_idxs)
    if raw_total <= 0:
        return 0, 1.0

    window_sec = _survey_window_seconds(resolved)
    target_window = int(round(daily_cars * window_sec / 86_400))
    global_scale = target_window / raw_total

    begins = {resolved[i].begin for i in cordon_idxs}
    interval_targets = _interval_targets_by_hourly_profile(
        begins, profile, target_window
    )
    for begin in begins:
        idxs = [i for i in cordon_idxs if resolved[i].begin == begin]
        interval_target = interval_targets.get(begin, 0)
        sec_counts = [resolved[i].car_count for i in idxs]
        for i, new_c in zip(
            idxs, _scale_counts_to_target(sec_counts, interval_target)
        ):
            resolved[i].car_count = new_c

    for begin in begins:
        idxs = [i for i in cordon_idxs if resolved[i].begin == begin]
        interval_total = sum(resolved[i].car_count for i in idxs)
        by_sector: dict[str, list[int]] = {k: [] for k in shares}
        for i in idxs:
            sec = sector(resolved[i].demand_from)
            if sec in by_sector:
                by_sector[sec].append(i)
        sector_targets = _share_targets(interval_total, shares)
        for sec, sec_idxs in by_sector.items():
            sec_counts = [resolved[i].car_count for i in sec_idxs]
            for i, new_c in zip(
                sec_idxs, _scale_counts_to_target(sec_counts, sector_targets.get(sec, 0))
            ):
                resolved[i].car_count = new_c

    after = sum(resolved[i].car_count for i in cordon_idxs)
    return max(0, raw_total - after), global_scale


def apply_ecan_bus_interchange_calibration(
    resolved: list[ResolvedTripRow],
    net,
    *,
    movements_per_hour: int = ECAN_INTERCHANGE_MOVEMENTS_PER_HOUR,
    daily_stops: int = ECAN_INTERCHANGE_DAILY_STOPS_TARGET,
    hourly_profile: tuple[int, ...] | None = None,
) -> tuple[int, float]:
    """
    Scale/cap bus trips to ECan interchange daily stops and hourly movement profile.

    Peaks use ~75–85 movements/h (below 96/h ceiling); off-peak ~45–55/h; late night ~25/h.
    Returns (buses trimmed, global scale factor).
    """
    profile = (
        hourly_profile
        if hourly_profile is not None
        else ECAN_INTERCHANGE_HOURLY_MOVEMENTS
    )
    bus_idxs = [i for i, rt in enumerate(resolved) if rt.bus_count > 0]
    raw_total = sum(resolved[i].bus_count for i in bus_idxs)
    if raw_total <= 0:
        return 0, 1.0

    window_sec = _survey_window_seconds(resolved)
    target_window = int(round(daily_stops * window_sec / 86_400))
    begins = {resolved[i].begin for i in bus_idxs}
    interval_targets = _interval_targets_by_hourly_profile(
        begins, profile, target_window
    )

    for begin in begins:
        idxs = [i for i in bus_idxs if resolved[i].begin == begin]
        interval_target = interval_targets.get(begin, 0)
        per_interval_cap = _interval_cap_from_hourly_profile(
            begin, profile, ceiling_per_hour=movements_per_hour
        )
        interval_target = min(interval_target, per_interval_cap)
        sec_counts = [resolved[i].bus_count for i in idxs]
        for i, new_b in zip(
            idxs, _scale_counts_to_target(sec_counts, interval_target)
        ):
            resolved[i].bus_count = new_b
    after = sum(resolved[i].bus_count for i in bus_idxs)
    scale = after / raw_total if raw_total else 1.0
    return max(0, raw_total - after), scale


def _resolve_trip_row_edges(
    row: dict[str, str],
    net,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    source_by_enter: dict[str, str],
    sink_by_exit: dict[str, str],
    from_map: dict[str, str],
    to_map: dict[str, str],
    dest_key3: dict[tuple[str, str, str], str],
    dest_key2: dict[tuple[str, str], str],
    dest_key1: dict[str, str],
    dead_junctions: set[str],
    coord_map: dict[str, tuple[float, float]] | None = None,
) -> ResolvedTripRow | None:
    """Resolve boundary OD once; empty from/to when clip/reachability fails."""
    car_count, bus_count = motor_vehicle_counts(row)
    if car_count <= 0 and bus_count <= 0:
        return None
    movement = (row.get("movement") or "").strip()
    dest_id, inferred = resolve_destination_id(row, dest_key3, dest_key2, dest_key1)
    origin_id = (row.get("intersection_id") or "").strip()
    if not dest_id and is_turn_movement(movement):
        from_edge, to_edge, approach_enter, dest_exit = resolve_turn_only_trip_edges(
            net,
            origin_id,
            row,
            enter_map,
            exit_map,
            junction_map,
            source_by_enter,
            from_map,
            dead_junctions,
            coord_map=coord_map,
        )
        inferred = False
    elif not dest_id:
        return None
    else:
        from_edge, to_edge, approach_enter, dest_exit = boundary_trip_edges(
            origin_id,
            dest_id,
            row,
            net,
            enter_map,
            exit_map,
            junction_map,
            source_by_enter,
            sink_by_exit,
            from_map,
            to_map,
            coord_map=coord_map,
            dead_junctions=dead_junctions,
        )
    if not from_edge or not to_edge:
        return None
    demand_from, demand_to = from_edge, to_edge
    clipped = resolve_trip_edges(
        from_edge, to_edge, origin_id, dest_id, from_map, to_map, net
    )
    if clipped == (None, None):
        write_from, write_to = "", ""
    else:
        write_from, write_to = clipped
    return ResolvedTripRow(
        row=row,
        car_count=car_count,
        bus_count=bus_count,
        origin_id=origin_id,
        dest_id=dest_id,
        inferred=inferred,
        movement=movement,
        begin=time_to_begin_sec(row.get("time", "")),
        approach_enter=approach_enter,
        dest_exit=dest_exit,
        demand_from=demand_from,
        demand_to=demand_to,
        from_edge=write_from,
        to_edge=write_to,
    )


_TRIP_WORKER: dict = {}


def _trip_worker_init(
    net_path: str,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    source_by_enter: dict[str, str],
    sink_by_exit: dict[str, str],
    from_map: dict[str, str],
    to_map: dict[str, str],
    dest_key3: dict[tuple[str, str, str], str],
    dest_key2: dict[tuple[str, str], str],
    dest_key1: dict[str, str],
    coord_map: dict[str, tuple[float, float]] | None,
) -> None:
    setup_sumolib()
    import sumolib.net as sumo_net  # noqa: E402

    net = sumo_net.readNet(net_path, withInternal=False)
    dead_junctions = {n.getID() for n in net.getNodes() if n.getType() == "dead_end"}
    dead_junctions |= BOUNDARY_DEAD_END_JUNCTION_IDS
    _TRIP_WORKER.clear()
    _TRIP_WORKER.update(
        net=net,
        enter_map=enter_map,
        exit_map=exit_map,
        junction_map=junction_map,
        source_by_enter=source_by_enter,
        sink_by_exit=sink_by_exit,
        from_map=from_map,
        to_map=to_map,
        dest_key3=dest_key3,
        dest_key2=dest_key2,
        dest_key1=dest_key1,
        dead_junctions=dead_junctions,
        coord_map=coord_map,
    )


def _trip_worker_resolve_batch(
    indexed_rows: list[tuple[int, dict[str, str]]],
) -> list[tuple[int, ResolvedTripRow | None]]:
    w = _TRIP_WORKER
    out: list[tuple[int, ResolvedTripRow | None]] = []
    for idx, row in indexed_rows:
        out.append(
            (
                idx,
                _resolve_trip_row_edges(
                    row,
                    w["net"],
                    w["enter_map"],
                    w["exit_map"],
                    w["junction_map"],
                    w["source_by_enter"],
                    w["sink_by_exit"],
                    w["from_map"],
                    w["to_map"],
                    w["dest_key3"],
                    w["dest_key2"],
                    w["dest_key1"],
                    w["dead_junctions"],
                    coord_map=w["coord_map"],
                ),
            )
        )
    return out


def _default_trip_workers(args) -> int:
    explicit = int(getattr(args, "trip_workers", 0) or 0)
    if explicit > 0:
        return explicit
    return min(4, max(1, (os.cpu_count() or 4) - 1))


def configure_trip_build_options(args, steps: list[str]) -> None:
    """Set embed_trip_routes and trip_workers from CLI + pipeline steps."""
    duarouter_runs = "duarouter" in steps
    args.embed_trip_routes = bool(
        getattr(args, "embed_trip_routes", False) or not duarouter_runs
    )
    args.trip_workers = _default_trip_workers(args)


def _resolve_trip_rows(
    indexed_rows: list[tuple[int, dict[str, str]]],
    net,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    source_by_enter: dict[str, str],
    sink_by_exit: dict[str, str],
    from_map: dict[str, str],
    to_map: dict[str, str],
    dest_key3: dict[tuple[str, str, str], str],
    dest_key2: dict[tuple[str, str], str],
    dest_key1: dict[str, str],
    dead_junctions: set[str],
    coord_map: dict[str, tuple[float, float]] | None,
    *,
    workers: int,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[ResolvedTripRow]:
    """Resolve rows in CSV order (order matters for demand capping)."""
    total = len(indexed_rows)
    if total == 0:
        return []

    def emit(done: int) -> None:
        if on_progress is not None:
            on_progress(done, total)

    if workers <= 1 or total < 200:
        resolved: list[ResolvedTripRow] = []
        for done, (_idx, row) in enumerate(indexed_rows, start=1):
            rt = _resolve_trip_row_edges(
                row,
                net,
                enter_map,
                exit_map,
                junction_map,
                source_by_enter,
                sink_by_exit,
                from_map,
                to_map,
                dest_key3,
                dest_key2,
                dest_key1,
                dead_junctions,
                coord_map=coord_map,
            )
            if rt is not None:
                resolved.append(rt)
            emit(done)
        return resolved

    chunk_size = max(64, total // (workers * 4))
    chunks = [
        indexed_rows[i : i + chunk_size]
        for i in range(0, total, chunk_size)
    ]
    initargs = (
        str(NET_XML),
        enter_map,
        exit_map,
        junction_map,
        source_by_enter,
        sink_by_exit,
        from_map,
        to_map,
        dest_key3,
        dest_key2,
        dest_key1,
        coord_map,
    )
    merged: list[tuple[int, ResolvedTripRow | None]] = []
    done = 0
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_trip_worker_init,
        initargs=initargs,
    ) as pool:
        for batch in pool.map(_trip_worker_resolve_batch, chunks):
            merged.extend(batch)
            done += len(batch)
            emit(min(done, total))
    merged.sort(key=lambda t: t[0])
    return [rt for _idx, rt in merged if rt is not None]


def _aggregate_edge_demands(
    resolved: list[ResolvedTripRow],
) -> tuple[
    dict[tuple[str, int], int],
    dict[tuple[str, int], int],
    dict[tuple[str, int], int],
    dict[tuple[str, int], int],
]:
    """Per (boundary edge, begin_sec): departure/arrival demand by vehicle type."""
    dep_car: dict[tuple[str, int], int] = {}
    dep_bus: dict[tuple[str, int], int] = {}
    arr_car: dict[tuple[str, int], int] = {}
    arr_bus: dict[tuple[str, int], int] = {}
    for rt in resolved:
        key_dep = (rt.demand_from, rt.begin)
        key_arr = (rt.demand_to, rt.begin)
        if rt.car_count > 0:
            dep_car[key_dep] = dep_car.get(key_dep, 0) + rt.car_count
            arr_car[key_arr] = arr_car.get(key_arr, 0) + rt.car_count
        if rt.bus_count > 0:
            dep_bus[key_dep] = dep_bus.get(key_dep, 0) + rt.bus_count
            arr_bus[key_arr] = arr_bus.get(key_arr, 0) + rt.bus_count
    return dep_car, dep_bus, arr_car, arr_bus


def _cached_trip_route_edges(
    cache: dict[tuple[str, str, str, str, str, str], list[str]],
    net,
    approach_enter: str,
    dest_exit: str,
    from_edge: str,
    to_edge: str,
    movement: str,
    vclass: str,
) -> list[str]:
    key = (approach_enter, dest_exit, from_edge, to_edge, movement, vclass)
    hit = cache.get(key)
    if hit is not None:
        return hit
    hit = build_trip_route_edges(
        net,
        approach_enter,
        dest_exit,
        from_edge,
        to_edge,
        movement=movement,
        vclass=vclass,
    )
    cache[key] = hit
    return hit


def resolve_trip_edges(
    from_edge: str,
    to_edge: str,
    origin_id: str,
    dest_id: str,
    from_map: dict[str, str],
    to_map: dict[str, str],
    net=None,
) -> tuple[str, str] | tuple[None, None]:
    """Use clipped dead-end ends; pick a reachable alternate when both ends coincide."""
    if not from_edge or not to_edge:
        return None, None
    if from_edge == to_edge:
        for alt in (to_map.get(dest_id), from_map.get(origin_id)):
            if alt and alt != from_edge:
                if net is None or edge_reachable(net, from_edge, alt):
                    return from_edge, alt
        return None, None
    return from_edge, to_edge


def compute_edge_demands(
    traffic_rows: list[dict[str, str]],
    net,
    enter_map: dict[str, str],
    exit_map: dict[str, str],
    junction_map: dict[str, str],
    source_by_enter: dict[str, str],
    sink_by_exit: dict[str, str],
    from_map: dict[str, str],
    to_map: dict[str, str],
    dest_key3: dict[tuple[str, str, str], str],
    dest_key2: dict[tuple[str, str], str],
    dest_key1: dict[str, str],
    intersections: frozenset[str] | None = None,
    coord_map: dict[str, tuple[float, float]] | None = None,
    *,
    on_row: Callable[[int, int], None] | None = None,
) -> tuple[dict[tuple[str, int], int], dict[tuple[str, int], int]]:
    """Per (boundary edge, begin_sec): departure and arrival demand."""
    dead_junctions = {n.getID() for n in net.getNodes() if n.getType() == "dead_end"}
    dead_junctions |= BOUNDARY_DEAD_END_JUNCTION_IDS
    resolved: list[ResolvedTripRow] = []
    row_total = len(traffic_rows)
    last_paint_at = 0.0
    last_phase_key = ""
    for row_i, row in enumerate(traffic_rows, start=1):
        if on_row is not None and _tick_refresh_due(
            row_i,
            row_total,
            last_paint_at,
            "edge_demands",
            last_phase_key,
        ):
            last_phase_key = "edge_demands"
            last_paint_at = time.perf_counter()
            on_row(row_i, row_total)
        if intersections is not None:
            origin_id = (row.get("intersection_id") or "").strip()
            dest_id = (row.get("destination_id") or "").strip()
            if not dest_id:
                dest_id, _ = resolve_destination_id(
                    row, dest_key3, dest_key2, dest_key1
                )
            if origin_id and dest_id:
                if (
                    origin_id not in intersections
                    or dest_id not in intersections
                ):
                    continue
        rt = _resolve_trip_row_edges(
            row,
            net,
            enter_map,
            exit_map,
            junction_map,
            source_by_enter,
            sink_by_exit,
            from_map,
            to_map,
            dest_key3,
            dest_key2,
            dest_key1,
            dead_junctions,
            coord_map=coord_map,
        )
        if rt is not None:
            resolved.append(rt)
    return _aggregate_edge_demands(resolved)


def cap_flow_count(
    from_edge: str,
    arr_edge: str,
    begin: int,
    count: int,
    vtype: str,
    dep_demands: dict[str, dict[tuple[str, int], int]],
    arr_demands: dict[str, dict[tuple[str, int], int]],
    used_dep: dict[str, dict[tuple[str, int], int]],
    used_arr: dict[str, dict[tuple[str, int], int]],
) -> int:
    """Limit sourcing/sinking per vehicle type at a boundary edge and interval."""
    dep_demand = dep_demands[vtype]
    arr_demand = arr_demands[vtype]
    dep_used = used_dep[vtype]
    arr_used = used_arr[vtype]
    from_key = (from_edge, begin)
    to_key = (arr_edge, begin)
    caps = [count]
    if from_key in dep_demand:
        caps.append(max(0, dep_demand[from_key] - dep_used.get(from_key, 0)))
    if to_key in arr_demand:
        caps.append(max(0, arr_demand[to_key] - arr_used.get(to_key, 0)))
    capped = min(caps)
    if capped > 0:
        dep_used[from_key] = dep_used.get(from_key, 0) + capped
        arr_used[to_key] = arr_used.get(to_key, 0) + capped
    return capped


def step_trips(args) -> int:
    print("=== Step: trips ===")
    if sys.gettrace() is not None:
        print(
            "Note: Python debugger is attached — the main process runs slowly. "
            "Trip resolution still uses background worker processes; for full speed "
            "run from a terminal: python create_demand.py",
            file=sys.stderr,
        )
    workers = int(getattr(args, "trip_workers", 1) or 1)
    embed_routes = bool(getattr(args, "embed_trip_routes", True))
    print(
        f"trip workers: {workers}; "
        f"embed <route> in trips XML: {embed_routes}"
    )
    if not EDGE_MAP_CSV.is_file():
        print("missing:", EDGE_MAP_CSV, file=sys.stderr)
        print("Run the map step first.", file=sys.stderr)
        return 1
    if not NET_XML.is_file():
        print("missing:", NET_XML, file=sys.stderr)
        print("Run the network step first.", file=sys.stderr)
        return 1

    traffic_csv = args.traffic
    if traffic_csv is None:
        traffic_csv = newest_traffic_csv()
    elif not traffic_csv.is_file():
        print("missing:", traffic_csv, file=sys.stderr)
        return 1

    prog = TripsStepProgress(
        enabled=not getattr(args, "no_progress", False),
        filename=traffic_csv.name,
    )
    phase_times: dict[str, float] = {}
    phase_t0 = time.perf_counter()

    enter_map, exit_map, junction_map, coord_map = load_intersection_maps(
        EDGE_MAP_CSV
    )
    (
        from_map,
        to_map,
        source_by_enter,
        sink_by_exit,
        source_edges,
        sink_edges,
        net,
    ) = build_boundary_edge_maps(
        NET_XML,
        enter_map,
        exit_map,
        junction_map=junction_map,
        on_tick=lambda i, t: prog.tick(
            "boundary maps", i, t, filename=NET_XML.name
        ),
    )
    phase_times["boundary maps"] = time.perf_counter() - phase_t0
    print(
        f"Boundary dead-end trip ends: {len(source_edges)} source edge(s), "
        f"{len(sink_edges)} sink edge(s)"
    )
    phase_t0 = time.perf_counter()
    prog.tick("read CSV", 0, 1)
    traffic_rows = read_traffic_rows(traffic_csv)
    prog.tick("read CSV", 1, 1)
    phase_times["read CSV"] = time.perf_counter() - phase_t0

    phase_t0 = time.perf_counter()
    prog.tick("infer destinations", 0, 1)
    dest_key3, dest_key2, dest_key1 = build_dest_inference(traffic_rows)
    prog.tick("infer destinations", 1, 1)
    phase_times["infer destinations"] = time.perf_counter() - phase_t0

    dead_junctions = {n.getID() for n in net.getNodes() if n.getType() == "dead_end"}
    dead_junctions |= BOUNDARY_DEAD_END_JUNCTION_IDS
    n_skip_no_map = 0
    n_skip_no_dest = 0
    n_skip_same = 0
    n_skip_zero = 0
    n_inferred = 0
    n_inferred_vehicles = 0
    phase_t0 = time.perf_counter()
    indexed_candidates: list[tuple[int, dict[str, str]]] = []
    row_total = len(traffic_rows)
    prog.tick("edge demands", 0, row_total, force=True)
    for row_i, row in enumerate(traffic_rows, start=1):
        prog.tick("edge demands", row_i, row_total)
        car_count, bus_count = motor_vehicle_counts(row)
        if car_count <= 0 and bus_count <= 0:
            n_skip_zero += 1
            continue
        movement = (row.get("movement") or "").strip()
        dest_id, inferred = resolve_destination_id(
            row, dest_key3, dest_key2, dest_key1
        )
        if not dest_id and not is_turn_movement(movement):
            n_skip_no_dest += 1
            continue
        if inferred:
            n_inferred += 1
            n_inferred_vehicles += car_count + bus_count
        indexed_candidates.append((row_i, row))
    resolved_trips = _resolve_trip_rows(
        indexed_candidates,
        net,
        enter_map,
        exit_map,
        junction_map,
        source_by_enter,
        sink_by_exit,
        from_map,
        to_map,
        dest_key3,
        dest_key2,
        dest_key1,
        dead_junctions,
        coord_map,
        workers=workers,
        on_progress=lambda done, total: prog.tick(
            "edge demands",
            min(row_total, row_total - total + done),
            row_total,
        ),
    )
    n_skip_no_map += len(indexed_candidates) - len(resolved_trips)
    trimmed_totals, trimmed_ix, trimmed_src, trimmed_pool = (
        apply_period_vehicle_caps(resolved_trips)
    )
    cordon_trimmed = 0
    cordon_scale = 1.0
    bus_trimmed = 0
    bus_scale = 1.0
    if getattr(args, "cordon_calibrate", True):
        cordon_trimmed, cordon_scale = apply_smartview_cordon_calibration(
            resolved_trips, net
        )
    if getattr(args, "bus_interchange_calibrate", True):
        bus_trimmed, bus_scale = apply_ecan_bus_interchange_calibration(
            resolved_trips, net
        )
    dep_car, dep_bus, arr_car, arr_bus = _aggregate_edge_demands(resolved_trips)
    dep_demand = {"car": dep_car, "bus": dep_bus}
    arr_demand = {"car": arr_car, "bus": arr_bus}
    prog.tick("edge demands", row_total, row_total, force=True)
    phase_times["edge demands"] = time.perf_counter() - phase_t0
    used_dep: dict[str, dict[tuple[str, int], int]] = {"car": {}, "bus": {}}
    used_arr: dict[str, dict[tuple[str, int], int]] = {"car": {}, "bus": {}}

    root = ET.Element("routes")
    ET.SubElement(root, "vType", id="car", vClass="passenger")
    ET.SubElement(root, "vType", id="bus", vClass="bus", color="0,122,135")
    bus_route_cache: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    trip_route_cache: dict[tuple[str, str, str, str, str, str], list[str]] = {}

    n_flow = 0
    n_capped = 0
    n_capped_vehicles = 0
    total_vehicles = 0
    total_buses = 0
    write_total = len(resolved_trips)
    phase_t0 = time.perf_counter()
    prog.tick("write flows", 0, write_total, force=True)

    for row_i, rt in enumerate(resolved_trips, start=1):
        prog.tick("write flows", row_i, write_total)
        row = rt.row
        car_count, bus_count = rt.car_count, rt.bus_count
        origin_id, dest_id = rt.origin_id, rt.dest_id
        inferred, movement = rt.inferred, rt.movement
        from_edge, to_edge = rt.from_edge, rt.to_edge
        if not from_edge or not to_edge:
            raw_same = from_map.get(origin_id) == to_map.get(dest_id)
            if raw_same and from_map.get(origin_id):
                n_skip_same += 1
            else:
                n_skip_no_map += 1
            continue
        approach_enter, dest_exit = rt.approach_enter, rt.dest_exit
        begin = rt.begin
        arr_edge = to_edge
        end = begin + INTERVAL_SEC
        fid_base = safe_id(
            origin_id,
            dest_id,
            "inf" if inferred else "",
            row.get("movement_index", ""),
            row.get("time", ""),
            row.get("movement", ""),
        )

        for vtype, count in (("car", car_count), ("bus", bus_count)):
            if count <= 0:
                continue
            vclass = "bus" if vtype == "bus" else "passenger"
            needs_embedded_route = (
                embed_routes
                or vtype == "bus"
                or not edge_connected_vclass(net, from_edge, to_edge, vclass)
            )
            route_edges: list[str] = []
            if needs_embedded_route:
                route_edges = _cached_trip_route_edges(
                    trip_route_cache,
                    net,
                    approach_enter,
                    dest_exit,
                    from_edge,
                    to_edge,
                    movement,
                    vclass,
                )
            if needs_embedded_route and not route_edges:
                n_skip_no_map += 1
                continue
            capped = cap_flow_count(
                rt.demand_from,
                rt.demand_to,
                begin,
                count,
                vtype,
                dep_demand,
                arr_demand,
                used_dep,
                used_arr,
            )
            if capped <= 0:
                n_capped += 1
                continue
            if capped < count:
                n_capped += 1
                n_capped_vehicles += count - capped

            # Cars: from/to only so duarouter builds lane-valid paths (no bus-only links).
            trip_route: list[str] = []
            if vtype == "bus":
                trip_route = apply_bus_interchange_to_route(
                    net,
                    from_edge,
                    to_edge,
                    route_edges,
                    route_cache=bus_route_cache,
                )
                if trip_route:
                    trip_route = repair_bus_block_turns(net, trip_route)
            flow_attrs: dict[str, str] = {
                "id": safe_id(fid_base, vtype),
                "type": vtype,
                "begin": str(begin),
                "end": str(end),
                "number": str(capped),
            }
            # Omit from/to when a full <route> is present — SUMO still treats them as
            # mandatory endpoints and warns when boundary stubs are not adjacent.
            if not trip_route:
                flow_attrs["from"] = from_edge
                flow_attrs["to"] = to_edge
            flow_el = ET.SubElement(root, "flow", flow_attrs)
            if trip_route:
                ET.SubElement(
                    flow_el,
                    "route",
                    edges=" ".join(trip_route),
                )
            n_flow += 1
            total_vehicles += capped
            if vtype == "bus":
                total_buses += capped

    phase_times["write flows"] = time.perf_counter() - phase_t0
    phase_times["total"] = sum(
        phase_times[k] for k in (
            "boundary maps",
            "read CSV",
            "infer destinations",
            "edge demands",
            "write flows",
        )
    )

    ET.ElementTree(root).write(
        OUT_TRIPS,
        encoding="UTF-8",
        xml_declaration=True,
    )

    print(f"Source CSV: {traffic_csv}")
    print(f"Wrote {OUT_TRIPS}")
    print(f"  flows: {n_flow}")
    print(f"  vehicles (sum of number): {total_vehicles} ({total_buses} buses)")
    if total_buses:
        print("  buses: pass through Bus Interchange internal roads (no platform stops)")
    print(f"  inferred destination (no dest in CSV): {n_inferred} flows, {n_inferred_vehicles} vehicles")
    print(f"  skipped (no destination, cannot infer): {n_skip_no_dest}")
    print(f"  skipped (no edge map): {n_skip_no_map}")
    print(f"  skipped (unresolved same from/to edge): {n_skip_same}")
    if trimmed_totals or trimmed_ix or trimmed_src or trimmed_pool:
        print(
            "  capped (period / totals budget): "
            f"{trimmed_totals + trimmed_ix + trimmed_src + trimmed_pool} "
            "vehicles trimmed "
            f"(row totals={trimmed_totals}, "
            f"intersection+interval={trimmed_ix}, "
            f"source+interval={trimmed_src}, "
            f"source totals pool={trimmed_pool})"
        )
    if getattr(args, "cordon_calibrate", True):
        window_h = _survey_window_seconds(resolved_trips) / 3600
        target_cordon = int(
            round(CCC_SMARTVIEW_DAILY_CORDON_CARS * _survey_window_seconds(resolved_trips) / 86_400)
        )
        am_peak = max(CCC_CORDON_HOURLY_VPH[7:9])
        pm_peak = max(CCC_CORDON_HOURLY_VPH[16:18])
        plateau = sum(CCC_CORDON_HOURLY_VPH[10:15]) // 5
        print(
            "  cordon calibration (CCC SmartView): "
            f"target ~{target_cordon:,} car entries over {window_h:.1f} h survey window "
            f"(daily {CCC_SMARTVIEW_DAILY_CORDON_CARS:,}); "
            f"M-curve profile AM/PM peak ~{am_peak:,}/{pm_peak:,} veh/h, "
            f"plateau ~{plateau:,} veh/h; "
            f"scale {cordon_scale:.3f}; "
            f"trimmed {cordon_trimmed:,}; "
            f"shares S/N/E/W "
            f"{CCC_CORDON_DIRECTION_SHARE['south']:.1%}/"
            f"{CCC_CORDON_DIRECTION_SHARE['north']:.1%}/"
            f"{CCC_CORDON_DIRECTION_SHARE['east']:.1%}/"
            f"{CCC_CORDON_DIRECTION_SHARE['west']:.1%}"
        )
    if getattr(args, "bus_interchange_calibrate", True):
        target_bus = int(
            round(
                ECAN_INTERCHANGE_DAILY_STOPS_TARGET
                * _survey_window_seconds(resolved_trips)
                / 86_400
            )
        )
        bus_am = max(ECAN_INTERCHANGE_HOURLY_MOVEMENTS[7:9])
        bus_pm = max(ECAN_INTERCHANGE_HOURLY_MOVEMENTS[16:18])
        bus_mid = sum(ECAN_INTERCHANGE_HOURLY_MOVEMENTS[10:15]) // 5
        print(
            "  bus interchange calibration (ECan): "
            f"target ~{target_bus:,} movements over survey window "
            f"(daily stops ~{ECAN_INTERCHANGE_DAILY_STOPS_TARGET:,}, "
            f"hourly profile AM/PM ~{bus_am}/{bus_pm} mov/h, midday ~{bus_mid}/h, "
            f"ceiling {ECAN_INTERCHANGE_MOVEMENTS_PER_HOUR}/h); "
            f"scale {bus_scale:.3f}; trimmed {bus_trimmed:,}"
        )
    print(f"  capped (edge demand limit): {n_capped} flows, {n_capped_vehicles} vehicles trimmed")
    print(f"  skipped (no motor vehicles): {n_skip_zero}")
    print("  phase timing:")
    for name in (
        "boundary maps",
        "read CSV",
        "infer destinations",
        "edge demands",
        "write flows",
        "total",
    ):
        print(f"    {name}: {_fmt_duration(phase_times[name])}")
    prog.dismiss()
    prog.finish("COMPLETED: trips")
    return 0


# --- Step 4: duarouter -------------------------------------------------------


_FLOW_TAG_RE = re.compile(
    r'<flow id="([^"]+)"([^>]*)><route edges="([^"]+)"\s*/>\s*</flow>'
)


def _routed_flow_ids(routed_path: Path) -> set[str]:
    """Flow ids that already have at least one vehicle in the routed file."""
    ids: set[str] = set()
    for _ev, el in ET.iterparse(routed_path, events=("end",)):
        if el.tag != "vehicle":
            continue
        vid = el.get("id") or ""
        if "." in vid:
            ids.add(vid.rsplit(".", 1)[0])
        elif vid:
            ids.add(vid)
        el.clear()
    return ids


def _embedded_trip_flows(trips_path: Path) -> list[dict[str, str | int]]:
    """Flows in trips XML that include a pre-built <route> edge list."""
    text = trips_path.read_text(encoding="utf-8")
    flows: list[dict[str, str | int]] = []
    for m in _FLOW_TAG_RE.finditer(text):
        attrs = {
            a: v
            for a, v in re.findall(r'(\w+)="([^"]*)"', m.group(2))
        }
        flows.append(
            {
                "id": m.group(1),
                "type": attrs.get("type", "car"),
                "begin": int(attrs.get("begin") or 0),
                "end": int(attrs.get("end") or 0),
                "number": int(attrs.get("number") or 0),
                "edges": m.group(3).strip(),
            }
        )
    return flows


def _vehicle_xml_for_flow(flow: dict[str, str | int]) -> list[str]:
    """Expand one demand flow into per-vehicle XML lines."""
    flow_id = str(flow["id"])
    vtype = str(flow["type"])
    begin = int(flow["begin"])
    end = int(flow["end"])
    number = int(flow["number"])
    edges = str(flow["edges"])
    if number <= 0 or not edges:
        return []
    span = max(end - begin, 1)
    lines: list[str] = []
    for i in range(number):
        depart = begin + (i + 0.5) * span / number
        vid = f"{flow_id}.{i}"
        lines.append(
            f'    <vehicle id="{vid}" type="{vtype}" depart="{depart:.2f}">\n'
            f'        <route edges="{edges}" />\n'
            f"    </vehicle>"
        )
    return lines


def _sort_route_file_vehicles_by_depart(root: ET.Element) -> None:
    """Reorder <vehicle> elements by increasing depart (SUMO requirement)."""
    vehicles = [c for c in list(root) if c.tag == "vehicle"]
    if len(vehicles) < 2:
        return
    for veh in vehicles:
        root.remove(veh)
    vehicles.sort(key=lambda el: float(el.get("depart") or 0))
    for veh in vehicles:
        root.append(veh)


def count_trips_flows_without_route(trips_path: Path) -> int:
    """Flows that still rely on duarouter (no embedded <route> edges)."""
    n = 0
    for _ev, el in ET.iterparse(trips_path, events=("end",)):
        if el.tag != "flow":
            continue
        route_el = el.find("route")
        if route_el is None or not (route_el.get("edges") or "").strip():
            n += 1
        el.clear()
    return n


def expand_embedded_trips_to_routed(
    trips_path: Path = OUT_TRIPS,
    routed_path: Path = OUT_ROUTED,
) -> int:
    """Build routed demand from embedded trip routes (skip duarouter warnings)."""
    tree = ET.parse(trips_path)
    in_root = tree.getroot()
    out_root = ET.Element("routes")
    for vtype in in_root.findall("vType"):
        out_root.append(
            ET.Element("vType", attrib=dict(vtype.attrib))
        )

    n_vehicles = 0
    n_skip = 0
    for flow in in_root.findall("flow"):
        route_el = flow.find("route")
        edges = (route_el.get("edges") if route_el is not None else "") or ""
        if not edges.strip():
            n_skip += 1
            continue
        flow_info: dict[str, str | int] = {
            "id": flow.get("id") or "",
            "type": flow.get("type") or "car",
            "begin": int(flow.get("begin") or 0),
            "end": int(flow.get("end") or 0),
            "number": int(flow.get("number") or 0),
            "edges": edges.strip(),
        }
        span = max(int(flow_info["end"]) - int(flow_info["begin"]), 1)
        number = int(flow_info["number"])
        for i in range(number):
            depart = int(flow_info["begin"]) + (i + 0.5) * span / number
            veh = ET.SubElement(
                out_root,
                "vehicle",
                {
                    "id": f"{flow_info['id']}.{i}",
                    "type": str(flow_info["type"]),
                    "depart": f"{depart:.2f}",
                },
            )
            ET.SubElement(veh, "route", edges=str(flow_info["edges"]))
            n_vehicles += 1

    _sort_route_file_vehicles_by_depart(out_root)
    out_tree = ET.ElementTree(out_root)
    if hasattr(ET, "indent"):
        ET.indent(out_tree, space="    ")
    out_tree.write(
        routed_path,
        encoding="UTF-8",
        xml_declaration=True,
    )
    if n_skip:
        print(
            f"warning: {n_skip} trip flow(s) without embedded route were skipped",
            file=sys.stderr,
        )
    return n_vehicles


def append_missing_embedded_flows_to_routed(
    trips_path: Path = OUT_TRIPS,
    routed_path: Path = OUT_ROUTED,
) -> int:
    """Add bus vehicles duarouter skipped but trips already has embedded routes for."""
    embedded = [
        f
        for f in _embedded_trip_flows(trips_path)
        if str(f.get("type") or "") == "bus"
    ]
    if not embedded:
        return 0
    present = _routed_flow_ids(routed_path)
    missing = [f for f in embedded if str(f["id"]) not in present]
    if not missing:
        return 0

    chunks: list[str] = []
    n_vehicles = 0
    for flow in missing:
        veh_lines = _vehicle_xml_for_flow(flow)
        chunks.extend(veh_lines)
        n_vehicles += len(veh_lines)

    text = routed_path.read_text(encoding="utf-8")
    insert = "\n".join(chunks) + "\n"
    if "</routes>" in text:
        text = text.replace("</routes>", insert + "</routes>", 1)
    else:
        text += insert
    routed_path.write_text(text, encoding="utf-8")
    tree = ET.parse(routed_path)
    _sort_route_file_vehicles_by_depart(tree.getroot())
    if hasattr(ET, "indent"):
        ET.indent(tree, space="    ")
    tree.write(
        routed_path,
        encoding="UTF-8",
        xml_declaration=True,
    )
    return n_vehicles


def _embedded_flow_routes_from_trips(
    trips_path: Path,
    *,
    vtypes: frozenset[str] | None = None,
) -> dict[str, str]:
    """Map flow id -> embedded route edges (optionally filtered by vType)."""
    routes: dict[str, str] = {}
    for _ev, el in ET.iterparse(trips_path, events=("end",)):
        if el.tag != "flow":
            continue
        if vtypes is not None and (el.get("type") or "") not in vtypes:
            continue
        route_el = el.find("route")
        edges = (route_el.get("edges") if route_el is not None else "") or ""
        if edges.strip() and el.get("id"):
            routes[el.get("id")] = edges.strip()
        el.clear()
    return routes


def apply_embedded_flow_routes_to_routed(
    trips_path: Path = OUT_TRIPS,
    routed_path: Path = OUT_ROUTED,
) -> int:
    """Keep bus interchange embedded routes; cars stay on duarouter paths."""
    flow_routes = _embedded_flow_routes_from_trips(trips_path, vtypes=frozenset({"bus"}))
    if not flow_routes:
        return 0

    tree = ET.parse(routed_path)
    root = tree.getroot()
    updated = 0
    for vehicle in root.findall("vehicle"):
        vid = vehicle.get("id") or ""
        if "." not in vid:
            continue
        flow_id, _idx = vid.rsplit(".", 1)
        edges = flow_routes.get(flow_id)
        if not edges:
            continue
        route_el = vehicle.find("route")
        if route_el is None:
            route_el = ET.SubElement(vehicle, "route")
        if route_el.get("edges") != edges:
            route_el.set("edges", edges)
            updated += 1

    if updated:
        if hasattr(ET, "indent"):
            ET.indent(tree, space="    ")
        tree.write(
            routed_path,
            encoding="UTF-8",
            xml_declaration=True,
        )
    return updated


def _remove_stale_duarouter_alt() -> None:
    """Drop legacy route-alternatives file (not loaded by sumocfg)."""
    if OUT_ROUTED_ALT.is_file():
        OUT_ROUTED_ALT.unlink()
        print(f"removed unused route alternatives -> {OUT_ROUTED_ALT.name}")


def _strip_car_routes_from_trips(trips_path: Path) -> int:
    """Remove embedded <route> from car flows so duarouter does not reuse bad paths."""
    tree = ET.parse(trips_path)
    root = tree.getroot()
    removed = 0
    for flow in root.findall("flow"):
        if (flow.get("type") or "") != "car":
            continue
        route_el = flow.find("route")
        if route_el is not None:
            flow.remove(route_el)
            removed += 1
    if removed:
        if hasattr(ET, "indent"):
            ET.indent(tree, space="    ")
        tree.write(trips_path, encoding="UTF-8", xml_declaration=True)
    return removed


def _route_edges_connected(net, edges: list[str], vclass: str) -> bool:
    if len(edges) < 2:
        return bool(edges)
    allow_uturn = vclass_allow_uturn(vclass)
    for i in range(len(edges) - 1):
        if not _lane_pair_connected_vclass(
            net,
            edges[i],
            edges[i + 1],
            vclass,
            allow_uturn=allow_uturn,
        ):
            return False
    return True


def _validate_repair_vehicle_routes(
    net, vehicle: ET.Element, vclass: str
) -> tuple[bool, bool]:
    """Repair or prune a vehicle route / routeDistribution. Returns (keep, changed)."""
    rd = vehicle.find("routeDistribution")
    if rd is not None:
        changed = False
        valid: list[ET.Element] = []
        index_map: dict[int, int] = {}
        for old_i, route_el in enumerate(list(rd.findall("route"))):
            raw = (route_el.get("edges") or "").strip()
            if not raw:
                rd.remove(route_el)
                changed = True
                continue
            edges = raw.split()
            if not _route_edges_connected(net, edges, vclass):
                fixed = _repair_route_edge_list(net, edges, vclass)
                if fixed and _route_edges_connected(net, fixed, vclass):
                    new_raw = " ".join(fixed)
                    if new_raw != raw:
                        route_el.set("edges", new_raw)
                        changed = True
                else:
                    rd.remove(route_el)
                    changed = True
                    continue
            index_map[old_i] = len(valid)
            valid.append(route_el)
        if not valid:
            return False, changed
        if len(valid) == 1:
            vehicle.remove(rd)
            ET.SubElement(vehicle, "route", edges=valid[0].get("edges", ""))
            return True, True
        last_raw = rd.get("last")
        last_idx = int(last_raw) if last_raw is not None else 0
        new_last = index_map.get(last_idx)
        if new_last is None:
            new_last = 0
            changed = True
        if rd.get("last") != str(new_last):
            rd.set("last", str(new_last))
            changed = True
        probs = [float(r.get("probability") or 0.0) for r in valid]
        total = sum(probs)
        if total <= 0:
            share = 1.0 / len(valid)
            for route_el in valid:
                route_el.set("probability", f"{share:.8f}")
            changed = True
        else:
            for route_el, prob in zip(valid, probs):
                norm = prob / total
                old = route_el.get("probability") or ""
                new = f"{norm:.8f}"
                if old != new:
                    route_el.set("probability", new)
                    changed = True
        return True, changed

    route_el = vehicle.find("route")
    if route_el is None:
        return True, False
    raw = (route_el.get("edges") or "").strip()
    if not raw:
        return False, False
    edges = raw.split()
    if _route_edges_connected(net, edges, vclass):
        return True, False
    fixed = _repair_route_edge_list(net, edges, vclass)
    if not fixed or not _route_edges_connected(net, fixed, vclass):
        return False, False
    new_raw = " ".join(fixed)
    route_el.set("edges", new_raw)
    return True, new_raw != raw


def validate_repair_routed_routes(
    net_path: Path = NET_XML,
    routed_path: Path = OUT_ROUTED,
) -> tuple[int, int, int]:
    """
    Check every vehicle route for vClass connectivity; repair or drop invalid.

    Handles plain <route> and duarouter <routeDistribution> alternatives.

    Returns (checked, repaired, dropped).
    """
    setup_sumolib()
    import sumolib

    net = sumolib.net.readNet(str(net_path))
    tree = ET.parse(routed_path)
    root = tree.getroot()
    checked = repaired = dropped = 0
    to_remove: list[ET.Element] = []
    for vehicle in root.findall("vehicle"):
        vtype = vehicle.get("type") or "car"
        vclass = "bus" if vtype == "bus" else "passenger"
        if vehicle.find("route") is None and vehicle.find("routeDistribution") is None:
            continue
        checked += 1
        keep, changed = _validate_repair_vehicle_routes(net, vehicle, vclass)
        if not keep:
            to_remove.append(vehicle)
            dropped += 1
        elif changed:
            repaired += 1
    for veh in to_remove:
        root.remove(veh)
    for route_el in root.findall("route"):
        if route_el.get("id") is None:
            continue
        raw = (route_el.get("edges") or "").strip()
        if not raw:
            continue
        vclass = "bus"
        checked += 1
        edges = raw.split()
        if _route_edges_connected(net, edges, vclass):
            continue
        fixed = _repair_route_edge_list(net, edges, vclass)
        if fixed and _route_edges_connected(net, fixed, vclass):
            new_raw = " ".join(fixed)
            if new_raw != raw:
                route_el.set("edges", new_raw)
                repaired += 1
        else:
            root.remove(route_el)
            dropped += 1
    if repaired or dropped:
        _sort_route_file_vehicles_by_depart(root)
        if hasattr(ET, "indent"):
            ET.indent(tree, space="    ")
        tree.write(routed_path, encoding="UTF-8", xml_declaration=True)
    return checked, repaired, dropped


def preflight_routed_demand(
    net_path: Path = NET_XML,
    routed_path: Path = OUT_ROUTED,
    *,
    sample_limit: int = 10,
) -> tuple[int, int, list[tuple[str, str, str]]]:
    """
    Scan routed vehicles for consecutive edges that SUMO cannot connect (vclass-aware).

    Returns (vehicles_checked, invalid_hop_count, sample (id, from, to) triples).
    Does not modify files.
    """
    if not routed_path.is_file():
        return 0, 0, []
    setup_sumolib()
    import sumolib

    global _NET_LANE_PERM_CACHE
    _NET_LANE_PERM_CACHE = None
    net = sumolib.net.readNet(str(net_path))
    _net_lane_perm_cache(net_path)
    root = ET.parse(routed_path).getroot()
    checked = 0
    bad: list[tuple[str, str, str]] = []
    for vehicle in root.findall("vehicle"):
        vtype = vehicle.get("type") or "car"
        vclass = "bus" if vtype == "bus" else "passenger"
        route_lists: list[list[str]] = []
        route_el = vehicle.find("route")
        if route_el is not None:
            route_lists.append((route_el.get("edges") or "").split())
        rd = vehicle.find("routeDistribution")
        if rd is not None:
            last_raw = rd.get("last")
            routes = rd.findall("route")
            if routes:
                idx = int(last_raw) if last_raw is not None else len(routes) - 1
                idx = max(0, min(idx, len(routes) - 1))
                route_lists.append((routes[idx].get("edges") or "").split())
        for edges in route_lists:
            if not edges:
                continue
            checked += 1
            for i in range(len(edges) - 1):
                if not _lane_pair_connected_vclass(
                    net,
                    edges[i],
                    edges[i + 1],
                    vclass,
                    allow_uturn=vclass_allow_uturn(vclass),
                ):
                    bad.append((vehicle.get("id") or "", edges[i], edges[i + 1]))
                    break
            break
    return checked, len(bad), bad[:sample_limit]


def step_duarouter(args) -> int:
    print("=== Step: duarouter ===")
    if not OUT_TRIPS.is_file():
        print("missing:", OUT_TRIPS, file=sys.stderr)
        print("Run the trips step first.", file=sys.stderr)
        return 1
    if not NET_XML.is_file():
        print("missing:", NET_XML, file=sys.stderr)
        return 1

    _remove_stale_duarouter_alt()
    stripped = _strip_car_routes_from_trips(OUT_TRIPS)
    if stripped:
        print(
            f"stripped embedded <route> from {stripped} car flow(s) in {OUT_TRIPS.name} "
            "(duarouter will route cars from from/to)"
        )
    missing_routes = count_trips_flows_without_route(OUT_TRIPS)
    use_expand = bool(getattr(args, "embed_trip_routes", False))
    if use_expand:
        prog = step_progress("ROUTING", args)
        n_vehicles = expand_embedded_trips_to_routed()
        prog.dismiss()
        prog.finish("COMPLETED: duarouter")
        print(
            f"expanded embedded trip routes -> {n_vehicles} vehicle(s) in "
            f"{OUT_ROUTED.name}"
        )
        if missing_routes:
            print(
                f"  ({missing_routes} flow(s) without embedded route were skipped; "
                "re-run trips or use --embed-trip-routes)",
                file=sys.stderr,
            )
        return 0

    cmd = [
        sumo_bin("duarouter"),
        "-n",
        str(NET_XML),
        "-r",
        str(OUT_TRIPS),
        "-o",
        str(OUT_ROUTED),
        "--alternatives-output",
        os.devnull,
        "--max-alternatives",
        "1",
        "--ignore-errors",
    ]
    if getattr(args, "routing_threads", None):
        cmd.extend(["--routing-threads", str(args.routing_threads)])
    prog = step_progress("ROUTING", args)
    horizon = trip_routing_horizon_sec(OUT_TRIPS)

    def on_line(line: str) -> tuple[int, int] | None:
        return parse_duarouter_timestep(line, horizon)

    run_subprocess_with_progress(
        cmd,
        prog,
        OUT_TRIPS.name,
        cwd=ROOT,
        on_line=on_line,
        progress_total=horizon,
    )
    prog.dismiss()
    _remove_stale_duarouter_alt()
    n_appended = append_missing_embedded_flows_to_routed()
    if n_appended:
        print(
            f"appended embedded trip flows duarouter skipped: "
            f"{n_appended} vehicle(s) -> {OUT_ROUTED.name}"
        )
    n_embedded = apply_embedded_flow_routes_to_routed()
    if n_embedded:
        print(
            f"applied embedded trip routes (boundary / interchange paths): "
            f"{n_embedded} vehicle(s) -> {OUT_ROUTED.name}"
        )
    checked, repaired, dropped = validate_repair_routed_routes()
    if checked:
        print(
            f"route validation: checked {checked:,}, "
            f"repaired {repaired:,}, dropped {dropped:,} invalid vehicle(s)"
        )
    prog.finish("COMPLETED: duarouter")
    print(f"wrote routed demand -> {OUT_ROUTED}")
    return 0


# --- Pipeline runner -----------------------------------------------------------

STEP_FUNCS = {
    "network": step_network,
    "map": step_map,
    "trips": step_trips,
    "duarouter": step_duarouter,
}


def parse_steps(
    only: str | None,
    skip: set[str],
    *,
    allowed: tuple[str, ...],
) -> list[str]:
    if only:
        steps = [s.strip().lower() for s in only.split(",") if s.strip()]
        bad = [s for s in steps if s not in allowed]
        if bad:
            raise SystemExit(
                f"Unknown step(s): {', '.join(bad)}. "
                f"Choose from: {', '.join(allowed)}"
            )
        return steps
    return [s for s in allowed if s not in skip]


def add_common_args(
    p: argparse.ArgumentParser,
    *,
    step_choices: tuple[str, ...] | None = None,
) -> None:
    only_help = "Comma-separated steps to run (default: all for this script)"
    if step_choices:
        only_help += f". Choices: {', '.join(step_choices)}"
    p.add_argument("--only", metavar="STEPS", help=only_help)
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable live progress bars on stderr (plain log output only)",
    )
    p.add_argument(
        "--sumo-home",
        type=Path,
        metavar="DIR",
        help=f"SUMO install directory (default: {DEFAULT_SUMO_HOME})",
    )


def run_pipeline(
    args: argparse.Namespace,
    steps: list[str],
    *,
    done_message: str,
    run_hint: str | None = None,
) -> int:
    if not steps:
        print("No steps selected.", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    DEMAND_DIR.mkdir(parents=True, exist_ok=True)

    sumo_home = setup_sumolib(sumo_home=getattr(args, "sumo_home", None))
    if sumo_home is None:
        print("missing SUMO install (expected", DEFAULT_SUMO_HOME, ")", file=sys.stderr)
        return 1
    print(f"SUMO_HOME: {sumo_home}")

    print("Pipeline steps:", ", ".join(steps))
    if "trips" in steps:
        configure_trip_build_options(args, steps)
    pipeline_prog = step_progress("PIPELINE", args)
    if pipeline_prog.enabled:
        pipeline_prog.tick(steps[0], 0, len(steps))
    for step_i, name in enumerate(steps, start=1):
        pipeline_prog.dismiss()
        rc = STEP_FUNCS[name](args)
        if rc != 0:
            pipeline_prog.dismiss()
            return rc
        if pipeline_prog.enabled:
            pipeline_prog.tick(name, step_i, len(steps))
    pipeline_prog.dismiss()
    pipeline_prog.finish(done_message)

    if run_hint:
        print(f"\n{run_hint}")
    return 0
