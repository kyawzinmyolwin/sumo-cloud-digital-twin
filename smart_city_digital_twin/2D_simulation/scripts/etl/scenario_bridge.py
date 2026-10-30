"""
Bridge: ETL processed counts (in the object store) -> a counts CSV the SUMO
demand generator consumes.

The ETL writes one JSON document per (intersection, date) under
``processed-traffic-data/<intersection_id>/<survey_date>.json`` (see etl.ingest /
etl.query). ``sumo_demand_from_traffic_csv.py`` — and make_cbd_scenario.py on top
of it — read a *flat CSV* of parser rows. This module joins the two: gather every
intersection's rows for one survey date and write them out as that CSV, so the
"grab from Drive -> build a CBD scenario" path is a single automated step instead
of a hand-downloaded file.

Pure and AWS-free: it talks to a ``ReadableStore`` (get_json / list_keys), the
same contract etl.query uses, so it's unit-testable with an in-memory fake and
runs in Lambda/EC2/CLI with the S3-backed store. It writes plain CSV; it does no
traffic modelling and no filtering beyond selecting the date (period/time-window
slicing stays in make_cbd_scenario.py, which already owns it).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Protocol

from etl.query import PROCESSED_PREFIX, _date_from_key

# Columns the demand generator needs (intersection_id -> destination_id turning
# movements, summed on `totals`, timed by survey_date/time/period). Emitted first
# and in this order so the CSV is stable and generator-ready; any other parser
# fields present are appended after these.
_PREFERRED_ORDER = [
    "intersection_id", "latitude", "longitude",
    "street_1", "street_2", "street_3", "street_4",
    "survey_date", "period", "time", "movement_index",
    "approach_header_1", "approach_header_2", "approach_bound", "movement",
    "destination_id", "destination_latitude", "destination_longitude",
    "lights", "other_vehicles", "bicycles_on_road", "bicycles_crosswalk", "totals",
]


class ReadableStore(Protocol):
    def get_json(self, key: str) -> dict[str, Any] | None: ...
    def list_keys(self, prefix: str) -> list[str]: ...


def _intersection_from_key(key: str, prefix: str) -> str:
    """`<prefix>/<intersection>/<date>.json` -> `<intersection>` (or '')."""
    rest = key[len(prefix) + 1:] if key.startswith(prefix + "/") else key
    return rest.split("/", 1)[0] if "/" in rest else ""


def keys_for_date(store: ReadableStore, date: str, *, prefix: str = PROCESSED_PREFIX) -> list[str]:
    """Processed-object keys whose survey date matches ``date`` (across all intersections)."""
    return sorted(
        k for k in store.list_keys(f"{prefix}/")
        if k.endswith(".json") and _date_from_key(k) == date
    )


def available_dates(
    store: ReadableStore, *, prefix: str = PROCESSED_PREFIX
) -> dict[str, int]:
    """Every survey date present in the store, mapped to how many intersections
    have data for it. Empty dict means the ETL hasn't ingested anything yet."""
    counts: dict[str, int] = {}
    for k in store.list_keys(f"{prefix}/"):
        if k.endswith(".json"):
            counts[_date_from_key(k)] = counts.get(_date_from_key(k), 0) + 1
    return counts


def fetch_rows_for_date(
    store: ReadableStore, date: str, *, prefix: str = PROCESSED_PREFIX
) -> list[dict[str, Any]]:
    """All parser rows recorded on ``date``, concatenated across intersections.

    Reads one document per intersection that has data for the date. Rows are
    returned as-is (the parser's dicts); the generator keys on their columns.
    """
    rows: list[dict[str, Any]] = []
    for key in keys_for_date(store, date, prefix=prefix):
        doc = store.get_json(key)
        if doc:
            rows.extend(doc.get("rows", []) or [])
    return rows


def counts_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    """Stable CSV header: the preferred generator columns present, then any extras."""
    seen = {k for r in rows for k in r.keys()}
    ordered = [c for c in _PREFERRED_ORDER if c in seen]
    extras = sorted(seen - set(ordered))
    return ordered + extras


def write_counts_csv(rows: list[dict[str, Any]], dst: Path) -> int:
    """Write ``rows`` to ``dst`` as CSV; return the row count. Empty rows -> header only."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = counts_fieldnames(rows) or list(_PREFERRED_ORDER)
    with dst.open("w", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})
    return len(rows)


def _time_slots(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({(r.get("time") or "").strip() for r in rows if (r.get("time") or "").strip()})


def build_counts_csv_for_date(
    store: ReadableStore, date: str, dst: Path, *, prefix: str = PROCESSED_PREFIX
) -> dict[str, Any]:
    """Fetch one date's counts from the store and write the generator CSV.

    Returns a small summary (rows written, distinct intersections, time slots) so
    callers can show coverage before simulating.
    """
    rows = fetch_rows_for_date(store, date, prefix=prefix)
    n = write_counts_csv(rows, dst)
    intersections = sorted({str(r.get("intersection_id", "") or "") for r in rows} - {""})
    return {
        "date": date,
        "output": str(dst),
        "rows": n,
        "intersections": len(intersections),
        "time_slots": _time_slots(rows),
    }
