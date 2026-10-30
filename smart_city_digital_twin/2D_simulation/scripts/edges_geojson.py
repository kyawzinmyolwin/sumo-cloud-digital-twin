#!/usr/bin/env python3
"""
Export the SUMO net's road edges as WGS84 GeoJSON so you can SEE and CLICK them on
a map (geojson.io, or the dashboard overlay) and read each edge's id — the thing
you need for --close-edge / incident specs.

The net is UTM-georeferenced (its <location> carries netOffset + a UTM projParameter),
so this converts each edge's shape from network XY to lon/lat with a self-contained
UTM inverse — no SUMO/pyproj needed, so it runs in a bare checkout and is testable.
Internal/connector edges are skipped.

    python scripts/edges_geojson.py                       # -> data/output/network/edges.geojson
    python scripts/edges_geojson.py --check               # verify against the net's origBoundary
    python scripts/edges_geojson.py --sumocfg scenarios/am_2020.sumocfg -o /tmp/scn_edges.geojson

Then drag the .geojson onto https://geojson.io to click each edge and read its id,
or load it as a dashboard overlay (see the dashboard's edge-layer, if wired).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from _sim_root import NETWORK_DIR  # noqa: E402

DEFAULT_NET = NETWORK_DIR / "Christchurch_Central_City_main_streets.net.xml"

# WGS84 ellipsoid
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_K0 = 0.9996


def utm_to_lonlat(easting: float, northing: float, zone: int) -> tuple[float, float]:
    """UTM (metres, no false-northing / no +south — negative northing = southern)
    -> (lon, lat) degrees. Matches SUMO's projParameter '+proj=utm +zone=N +ellps=WGS84'."""
    x = easting - 500000.0
    y = northing
    e2 = _E2
    ep2 = e2 / (1 - e2)
    m = y / _K0
    mu = m / (_A * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
            + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
            + (151 * e1**3 / 96) * math.sin(6 * mu)
            + (1097 * e1**4 / 512) * math.sin(8 * mu))
    c1 = ep2 * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = _A / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    r1 = _A * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon0 = zone * 6 - 183
    lon = math.radians(lon0) + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / math.cos(phi1)
    return math.degrees(lon), math.degrees(lat)


def _parse_location(root) -> tuple[tuple[float, float], int]:
    """Return (netOffset(x,y), utm_zone) from the net's <location>."""
    loc = root.find("location")
    if loc is None:
        raise ValueError("net has no <location> — cannot geo-reference")
    ox, oy = (float(v) for v in loc.get("netOffset", "0,0").split(","))
    proj = loc.get("projParameter", "")
    zone = 0
    for tok in proj.split():
        if tok.startswith("+zone="):
            zone = int(tok.split("=", 1)[1])
    if not zone:
        raise ValueError(f"no UTM +zone in projParameter {proj!r}")
    return (ox, oy), zone


def net_shape_to_lonlat(pts_xy, net_offset, zone):
    """Network-XY shape points -> [[lon,lat], ...]. UTM = net_xy - netOffset."""
    ox, oy = net_offset
    return [list(utm_to_lonlat(x - ox, y - oy, zone)) for (x, y) in pts_xy]


def _edge_features(root, net_offset, zone):
    for edge in root.iter("edge"):
        eid = edge.get("id", "")
        if eid.startswith(":") or edge.get("function"):
            continue
        shape = edge.get("shape") or (edge.find("lane").get("shape") if edge.find("lane") is not None else None)
        if not shape:
            continue
        pts = [tuple(float(v) for v in pair.split(",")) for pair in shape.split()]
        coords = net_shape_to_lonlat(pts, net_offset, zone)
        yield {
            "type": "Feature",
            "properties": {"id": eid, "name": edge.get("name", "")},
            "geometry": {"type": "LineString", "coordinates": coords},
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export SUMO net edges as WGS84 GeoJSON.")
    ap.add_argument("--net", type=Path, default=None, help=f"Net file (default: {DEFAULT_NET.name}).")
    ap.add_argument("--sumocfg", type=Path, default=None, help="Read the net from this scenario config.")
    ap.add_argument("-o", "--output", type=Path, default=NETWORK_DIR / "edges.geojson",
                    help="Output GeoJSON path (default: data/output/network/edges.geojson).")
    ap.add_argument("--check", action="store_true",
                    help="Validate the projection against the net's origBoundary and exit.")
    args = ap.parse_args()

    if args.sumocfg:
        node = ET.parse(args.sumocfg).getroot().find(".//net-file")
        net_path = (args.sumocfg.parent / node.get("value")).resolve()
    else:
        net_path = args.net or DEFAULT_NET
    if not net_path.is_file():
        print(f"Error: net file not found: {net_path}", file=sys.stderr)
        return 1

    root = ET.parse(net_path).getroot()
    net_offset, zone = _parse_location(root)

    if args.check:
        # Validate by CENTRE, not corners: origBoundary is a lon/lat min-max bbox
        # while convBoundary is a UTM rectangle, and UTM axes rotate slightly vs
        # lon/lat, so the rectangle's corners are NOT the min-lon/min-lat points
        # (they differ by tens of metres inherently). The centres, however, coincide.
        loc = root.find("location")
        ob = [float(v) for v in loc.get("origBoundary").split(",")]  # minlon,minlat,maxlon,maxlat
        cb = [float(v) for v in loc.get("convBoundary").split(",")]  # minx,miny,maxx,maxy
        cx, cy = (cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2
        got = utm_to_lonlat(cx - net_offset[0], cy - net_offset[1], zone)
        exp = ((ob[0] + ob[2]) / 2, (ob[1] + ob[3]) / 2)
        print(f"centre: got {got[0]:.6f},{got[1]:.6f}  expected {exp[0]:.6f},{exp[1]:.6f}")
        err = max(abs(got[0] - exp[0]), abs(got[1] - exp[1]))
        print(f"centre error: {err:.6f} deg (~{err * 111000:.0f} m) "
              f"({'OK' if err < 1e-4 else 'TOO LARGE'})")
        return 0 if err < 1e-4 else 2

    features = list(_edge_features(root, net_offset, zone))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"type": "FeatureCollection", "features": features}),
                           encoding="utf-8")
    print(f"wrote {len(features)} edges -> {args.output}")
    print("view: drag it onto https://geojson.io (click an edge to read its id/name)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
