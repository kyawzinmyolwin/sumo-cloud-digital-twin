#!/usr/bin/env python3
"""Fill directional neighbour columns on intersection_geo.csv.

Default (**road_chain**): consecutive intersections along the same named road **only if**
the straight-line gap between them is within **--max-edge-m** (default 300 m). That is our
proxy for “directly linked” without centreline geometry: long hops (e.g. missing
intermediate sites in the table) are not linked.

Other modes (see --mode): octant + shared street only; octant + pure geometry.

Empty cell = no neighbour recorded in that wedge.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from _sim_root import DATA_OUTPUT_DIR

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


def bearing_deg(dx: float, dy: float) -> float:
    """Clockwise degrees from north, in [0, 360). dx=east, dy=north."""
    return math.degrees(math.atan2(dx, dy)) % 360.0


def sector_index(bearing: float) -> int:
    """N centred at 0°, 45° sectors. Return index into NEIGHBOUR_KEYS."""
    if bearing >= 337.5 or bearing < 22.5:
        return 4  # N
    k = int((bearing - 22.5) // 45)
    return [5, 6, 7, 0, 1, 2, 3][k]  # NE, E, SE, S, SW, W, NW


def _street_tokens(row: dict[str, str]) -> set[str]:
    """Normalised leg names from street_1…street_4 (non-empty, lowercased, stripped)."""
    out: set[str] = set()
    for k in ("street_1", "street_2", "street_3", "street_4"):
        s = (row.get(k) or "").strip()
        if s and s.lower() not in ("n/a", "na", "-"):
            out.add(s.lower())
    return out


def _share_street(a: dict[str, str], b: dict[str, str]) -> bool:
    return bool(_street_tokens(a) & _street_tokens(b))


def _pca_unit_axis(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """First principal axis (unit vector) for points (xs, ys); fallback E–W if degenerate."""
    n = len(xs)
    if n < 2:
        return (1.0, 0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    cxx = sum((xs[i] - mx) ** 2 for i in range(n)) / n
    cyy = sum((ys[i] - my) ** 2 for i in range(n)) / n
    cxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    a = (cxx - cyy) / 2.0
    b = math.sqrt(max(0.0, a * a + cxy * cxy))
    lam1 = (cxx + cyy) / 2.0 + b
    vx = lam1 - cyy
    vy = cxy
    if abs(vx) + abs(vy) < 1e-12:
        vx, vy = cxy, lam1 - cxx
    h = math.hypot(vx, vy)
    if h < 1e-12:
        return (1.0, 0.0)
    return (vx / h, vy / h)


def _build_road_chain_adjacency(
    rows: list[dict[str, str]],
    *,
    eps_sq: float,
    max_edge_m: float,
) -> dict[str, set[str]]:
    """Undirected adjacency: consecutive intersections along each shared street corridor."""
    # street_token -> list of (id, x, y, row_i)
    by_street: dict[str, list[tuple[str, float, float, int]]] = defaultdict(list)
    coord_by_id: dict[str, tuple[float, float]] = {}
    for i, row in enumerate(rows):
        try:
            x = float(row["X"])
            y = float(row["Y"])
        except (KeyError, ValueError):
            continue
        if math.isnan(x) or math.isnan(y):
            continue
        iid = (row.get("intersection_id") or "").strip().upper()
        if not iid:
            continue
        coord_by_id[iid] = (x, y)
        for tok in _street_tokens(row):
            by_street[tok].append((iid, x, y, i))

    adj: dict[str, set[str]] = defaultdict(set)

    for _tok, lst in by_street.items():
        if len(lst) < 2:
            continue
        xs = [t[1] for t in lst]
        ys = [t[2] for t in lst]
        ux, uy = _pca_unit_axis(xs, ys)

        def proj(t: tuple[str, float, float, int]) -> float:
            return t[1] * ux + t[2] * uy

        ordered = sorted(lst, key=lambda t: (proj(t), t[0]))
        for j in range(len(ordered) - 1):
            a_id, ax, ay, _ = ordered[j]
            b_id, bx, by, _ = ordered[j + 1]
            dx = bx - ax
            dy = by - ay
            d2 = dx * dx + dy * dy
            if d2 < eps_sq:
                continue
            if math.sqrt(d2) > max_edge_m:
                continue
            if a_id != b_id:
                adj[a_id].add(b_id)
                adj[b_id].add(a_id)

    return adj


def fill_octants_from_adjacency(
    rows: list[dict[str, str]],
    coord_by_id: dict[str, tuple[float, float]],
    adj: dict[str, set[str]],
) -> None:
    """Write nearest neighbour per octant from graph edges (by Euclidean distance)."""
    for row in rows:
        iid = (row.get("intersection_id") or "").strip().upper()
        if not iid or iid not in coord_by_id:
            for key in NEIGHBOUR_KEYS:
                row[key] = ""
            continue
        ax, ay = coord_by_id[iid]
        best: list[tuple[float, str] | None] = [None] * 8
        for nb in adj.get(iid, ()):
            bx, by = coord_by_id.get(nb, (math.nan, math.nan))
            if math.isnan(bx):
                continue
            dx = bx - ax
            dy = by - ay
            d2 = dx * dx + dy * dy
            if d2 < 1e-12:
                continue
            si = sector_index(bearing_deg(dx, dy))
            cand = (d2, nb)
            if best[si] is None or d2 < best[si][0]:
                best[si] = cand
        for idx, key in enumerate(NEIGHBOUR_KEYS):
            row[key] = best[idx][1] if best[idx] else ""


def fill_octants_geometry_or_shared(
    rows: list[dict[str, str]],
    pts: list[dict[str, Any]],
    *,
    eps_sq: float,
    geometry_only: bool,
) -> None:
    """Legacy: octant + optional shared-street filter + nearest distance."""
    for p in pts:
        if math.isnan(p["x"]):
            for key in NEIGHBOUR_KEYS:
                rows[p["i"]][key] = ""
            continue
        best: list[tuple[float, str] | None] = [None] * 8
        for q in pts:
            if q["i"] == p["i"]:
                continue
            if math.isnan(q["x"]):
                continue
            if not geometry_only and not _share_street(rows[p["i"]], rows[q["i"]]):
                continue
            dx = q["x"] - p["x"]
            dy = q["y"] - p["y"]
            d2 = dx * dx + dy * dy
            if d2 < eps_sq:
                continue
            si = sector_index(bearing_deg(dx, dy))
            cand = (d2, q["id"])
            if best[si] is None or d2 < best[si][0]:
                best[si] = cand
        for idx, key in enumerate(NEIGHBOUR_KEYS):
            rows[p["i"]][key] = best[idx][1] if best[idx] else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "csv_path",
        nargs="?",
        default=str(DATA_OUTPUT_DIR / "intersection_geo.csv"),
        help="Path to intersection_geo.csv (default: data/output/intersection_geo.csv)",
    )
    ap.add_argument(
        "--eps",
        type=float,
        default=1e-3,
        help="Ignore pairs closer than this many metres (coincident rows)",
    )
    ap.add_argument(
        "--mode",
        choices=("road_chain", "octant_shared", "geometry"),
        default="road_chain",
        help=(
            "road_chain: consecutive along each street corridor (default); "
            "octant_shared: nearest in octant with shared leg name; "
            "geometry: nearest in octant only."
        ),
    )
    ap.add_argument(
        "--max-edge-m",
        type=float,
        default=300.0,
        help=(
            "road_chain only: maximum segment length (metres) for a chain edge — "
            "links longer than this are skipped (default: 300)."
        ),
    )
    args = ap.parse_args()
    path = Path(args.csv_path).resolve()

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        base_fields = [c for c in reader.fieldnames if c not in NEIGHBOUR_KEYS]
        rows = list(reader)

    pts: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        try:
            x = float(row["X"])
            y = float(row["Y"])
        except (KeyError, ValueError):
            x = y = float("nan")
        iid = (row.get("intersection_id") or "").strip()
        pts.append({"i": i, "id": iid, "x": x, "y": y})

    eps_sq = args.eps ** 2

    if args.mode == "road_chain":
        adj = _build_road_chain_adjacency(
            rows, eps_sq=eps_sq, max_edge_m=args.max_edge_m
        )
        coord_by_id: dict[str, tuple[float, float]] = {}
        for row in rows:
            try:
                x = float(row["X"])
                y = float(row["Y"])
            except (KeyError, ValueError):
                continue
            if math.isnan(x) or math.isnan(y):
                continue
            iid = (row.get("intersection_id") or "").strip().upper()
            if iid:
                coord_by_id[iid] = (x, y)
        fill_octants_from_adjacency(rows, coord_by_id, adj)
    else:
        fill_octants_geometry_or_shared(
            rows,
            pts,
            eps_sq=eps_sq,
            geometry_only=(args.mode == "geometry"),
        )

    out_fields = base_fields + NEIGHBOUR_KEYS
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)

    print(
        f"Updated {path} ({len(rows)} rows) mode={args.mode}"
        + (
            f" max_edge_m={args.max_edge_m:g}"
            if args.mode == "road_chain"
            else ""
        )
    )


if __name__ == "__main__":
    main()
