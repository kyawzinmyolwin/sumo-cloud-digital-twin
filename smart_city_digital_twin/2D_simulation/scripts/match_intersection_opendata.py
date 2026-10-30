#!/usr/bin/env python3
"""
Match ``intersection_streets.csv`` ids (Ixxxx) to ``Road_Intersection_(OpenData).csv``
``RoadIntersectionID`` using street-name correspondence.

Writes:

- ``intersection_RoadIntersectionID_map.csv`` — one row per candidate (ambiguous ids repeated).
- ``intersection_geo.csv`` — same rows plus ``latitude``, ``longitude`` (WGS84) from OpenData ``X``, ``Y`` (NZGD2000 / NZTM, EPSG:2193).

Run from the project folder:

    python3 match_intersection_opendata.py
"""

from __future__ import annotations

import csv
from pathlib import Path

from _sim_root import DATA_INPUT_DIR, DATA_OUTPUT_DIR

try:
    from pyproj import Transformer

    _NZTM_TO_WGS84 = Transformer.from_crs(
        "EPSG:2193", "EPSG:4326", always_xy=True
    )
except ImportError:
    _NZTM_TO_WGS84 = None

# Short names in the traffic workbook table -> extra OpenData tokens they may correspond to.
_STREET_ALIASES: dict[str, tuple[str, ...]] = {
    "park": ("park terrace",),
    "hagley": ("hagley avenue",),
    "cranmer": ("cranmer square",),
    "latimer": ("latimer square",),
    "cathedral": ("cathedral square",),
}

# Known fixes where the workbook row count or spelling does not align with OpenData strings.
_MANUAL_INTERSECTION_ID: dict[str, tuple[int, str]] = {
    # Workbook lists four fragments but OpenData names three legs at the same junction.
    "I2605": (
        3530,
        "Armagh Street / Park Terrace / Rolleston Avenue",
    ),
    # Centre-line naming omits Cathedral Square on this diagonal bundle.
    "I2963": (
        3956,
        "Colombo Street / Hereford Street / High Street",
    ),
    # OpenData lists three arms; ``Cambridge Terrace`` does not appear on this centre-line row.
    "I2843": (
        3800,
        "Oxford Terrace / Lichfield Street / Durham Street South",
    ),
}


def _csv_row_strip_bom(row: dict[str, str]) -> dict[str, str]:
    """CSV exports sometimes prefix the first column name with a UTF-8 BOM."""
    return {k.lstrip("\ufeff").strip(): v for k, v in row.items()}


def _norm_token(s: str) -> str:
    return " ".join(s.strip().lower().split())


def _expand_short_for_match(short: str) -> frozenset[str]:
    """Return comparable normalised strings for one abbreviated street cell."""
    if not short.strip():
        return frozenset()
    base = _norm_token(short)
    variants = {base}
    extra = _STREET_ALIASES.get(base)
    if extra:
        variants |= set(extra)
    return frozenset(variants)


def _segment_tokens(segment: str) -> set[str]:
    """Significant tokens from one OpenData leg (e.g. 'Durham Street North')."""
    s = _norm_token(segment)
    for suf in (
        " road",
        " street",
        " avenue",
        " terrace",
        " lane",
        " place",
        " drive",
        " square",
        " crescent",
        " boulevard",
        " way",
        " close",
    ):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    return set(s.split()) if s else set()


def _short_covers_segment(short: str, segment: str) -> bool:
    """Whether abbreviated ``short`` describes the same leg as OpenData ``segment``."""
    if not short.strip():
        return True
    seg_l = _norm_token(segment)
    sh = _norm_token(short)
    if seg_l == sh:
        return True
    if seg_l.startswith(sh + " ") or seg_l.startswith(sh + "/"):
        return True
    stoks = _segment_tokens(segment)
    for cand in _expand_short_for_match(short):
        if not cand:
            continue
        if cand in seg_l:
            return True
        cwords = cand.split()
        if all(w in stoks for w in cwords):
            return True
    # Last resort: first word of multi-word short (e.g. 'Durham' for 'Durham South').
    for w in sh.split():
        if w in stoks and len(w) > 2:
            return True
    return False


def _row_matches_unordered(shorts: tuple[str, ...], intersection_name: str) -> bool:
    parts = [p.strip() for p in intersection_name.split(" / ") if p.strip()]
    active = [s for s in shorts if s.strip()]
    if len(active) != len(parts):
        return False
    used: set[int] = set()
    for sh in active:
        found = False
        for i, seg in enumerate(parts):
            if i in used:
                continue
            if _short_covers_segment(sh, seg):
                used.add(i)
                found = True
                break
        if not found:
            return False
    return len(used) == len(parts)


def load_opendata_xyz(path: Path) -> dict[int, tuple[float, float, str]]:
    """``RoadIntersectionID`` → easting, northing, intersection label."""
    out: dict[int, tuple[float, float, str]] = {}
    with path.open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            row = _csv_row_strip_bom(row)
            rid_s = row.get("RoadIntersectionID", "").strip()
            if not rid_s:
                continue
            xs = row.get("X", "").strip()
            ys = row.get("Y", "").strip()
            try:
                x_f = float(xs)
                y_f = float(ys)
            except ValueError:
                continue
            rid = int(rid_s)
            name = (row.get("IntersectionName") or "").strip()
            out[rid] = (x_f, y_f, name)
    return out


def nztm_xy_to_lat_lon(x: float, y: float) -> tuple[str, str]:
    """NZTM easting/northing → latitude°, longitude° strings (WGS84)."""
    if _NZTM_TO_WGS84 is None:
        return "", ""
    lon, lat = _NZTM_TO_WGS84.transform(x, y)
    return f"{lat:.8f}", f"{lon:.8f}"


def write_intersection_geo_csv(
    map_csv: Path,
    xyz_by_id: dict[int, tuple[float, float, str]],
    geo_out: Path,
) -> None:
    """Emit rows matching ``intersection_id,latitude,longitude,...,RoadIntersectionID,X,Y,...``."""
    fields = [
        "intersection_id",
        "latitude",
        "longitude",
        "street_1",
        "street_2",
        "street_3",
        "street_4",
        "RoadIntersectionID",
        "X",
        "Y",
        "IntersectionName",
    ]
    with map_csv.open(encoding="utf-8") as fin, geo_out.open(
        "w", encoding="utf-8", newline=""
    ) as fout:
        reader = csv.DictReader(fin)
        w = csv.DictWriter(fout, fieldnames=fields)
        w.writeheader()
        for row in reader:
            rid_s = (row.get("RoadIntersectionID") or "").strip()
            lat_s = lon_s = x_s = y_s = name_out = ""
            if rid_s:
                try:
                    rid = int(rid_s)
                except ValueError:
                    rid = -1
                meta = xyz_by_id.get(rid)
                if meta is not None:
                    xf, yf, nm = meta
                    lat_s, lon_s = nztm_xy_to_lat_lon(xf, yf)
                    x_s = str(xf)
                    y_s = str(yf)
                    name_out = nm or (row.get("IntersectionName") or "").strip()
            w.writerow(
                {
                    "intersection_id": row["intersection_id"],
                    "latitude": lat_s,
                    "longitude": lon_s,
                    "street_1": row["street_1"],
                    "street_2": row["street_2"],
                    "street_3": row["street_3"],
                    "street_4": row["street_4"],
                    "RoadIntersectionID": rid_s,
                    "X": x_s,
                    "Y": y_s,
                    "IntersectionName": name_out,
                }
            )


def _iter_matches(
    open_rows: list[tuple[int, str]],
    iid: str,
    streets: tuple[str, str, str, str],
) -> list[tuple[int, str]]:
    if iid in _MANUAL_INTERSECTION_ID:
        rid, name = _MANUAL_INTERSECTION_ID[iid]
        return [(rid, name)]
    out: list[tuple[int, str]] = []
    for rid, name in open_rows:
        if _row_matches_unordered(streets, name):
            out.append((rid, name))
    return out


def main() -> None:
    open_path = DATA_INPUT_DIR / "Road_Intersection_(OpenData).csv"
    streets_path = DATA_INPUT_DIR / "intersection_streets.csv"
    out_path = DATA_OUTPUT_DIR / "intersection_RoadIntersectionID_map.csv"
    geo_path = DATA_OUTPUT_DIR / "intersection_geo.csv"

    open_rows: list[tuple[int, str]] = []
    with open_path.open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            row = _csv_row_strip_bom(row)
            rid = row.get("RoadIntersectionID", "").strip()
            name = row.get("IntersectionName", "").strip()
            if rid:
                open_rows.append((int(rid), name))

    # De-duplicate by (id, streets) for reporting
    id_streets: list[tuple[str, tuple[str, str, str, str]]] = []
    with streets_path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            iid = (row.get("id") or "").strip().upper()
            if not iid:
                continue
            s = tuple(row.get(f"street_{k}", "").strip() for k in (1, 2, 3, 4))
            id_streets.append((iid, s))

    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "intersection_id",
                "street_1",
                "street_2",
                "street_3",
                "street_4",
                "RoadIntersectionID",
                "IntersectionName",
                "status",
            ]
        )
        for iid, streets in id_streets:
            cands = _iter_matches(open_rows, iid, streets)
            if len(cands) == 1:
                rid, name = cands[0]
                st = "matched" if iid not in _MANUAL_INTERSECTION_ID else "manual_override"
                w.writerow([iid, *streets, rid, name, st])
            elif len(cands) > 1:
                for rid, name in cands:
                    w.writerow([iid, *streets, rid, name, "ambiguous"])
            else:
                w.writerow([iid, *streets, "", "", "unmatched"])

    xyz_by_id = load_opendata_xyz(open_path)
    write_intersection_geo_csv(out_path, xyz_by_id, geo_path)
    if _NZTM_TO_WGS84 is None:
        print(
            "Warning: pyproj is not installed; latitude and longitude are blank in "
            f"{geo_path.name} (X and Y are still written). "
            "Install with: pip install pyproj"
        )

    # Console summary
    n_ok = 0
    n_amb = 0
    n_fail = 0
    seen: set[str] = set()
    with out_path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            iid = row["intersection_id"]
            if iid in seen:
                continue
            seen.add(iid)
            st = row["status"]
            if st == "ambiguous":
                n_amb += 1
            elif st == "unmatched":
                n_fail += 1
            else:
                n_ok += 1

    print(f"Wrote {out_path.name}")
    print(f"Wrote {geo_path.name}")
    print(f"  Resolved (matched + manual): {n_ok}")
    print(f"  Unmatched ids: {n_fail}")
    print(f"  Ids with multiple OpenData hits: {n_amb}")


if __name__ == "__main__":
    main()
