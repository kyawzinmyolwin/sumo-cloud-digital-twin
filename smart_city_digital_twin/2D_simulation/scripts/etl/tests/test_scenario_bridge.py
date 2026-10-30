"""
Unit tests for the ETL -> scenario CSV bridge — in-memory store, no AWS, no SUMO.

    python -m pytest etl/tests/test_scenario_bridge.py
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from etl.scenario_bridge import (  # noqa: E402
    available_dates,
    build_counts_csv_for_date,
    counts_fieldnames,
    fetch_rows_for_date,
    keys_for_date,
    write_counts_csv,
)

P = "processed-traffic-data"


class MemStore:
    def __init__(self):
        self.docs = {}

    def get_json(self, key):
        return self.docs.get(key)

    def list_keys(self, prefix):
        return [k for k in self.docs if k.startswith(prefix)]


def _row(iid, dst, total, t="07:00"):
    return {"intersection_id": iid, "destination_id": dst, "totals": total,
            "survey_date": "2025-08-13", "time": t, "period": "AM"}


def _store():
    s = MemStore()
    # two intersections surveyed on the target date, one on a different date
    s.docs[f"{P}/I2537/2025-08-13.json"] = {
        "rowCount": 2, "rows": [_row("I2537", "I2554", 10), _row("I2537", "I2605", 5, "07:15")]}
    s.docs[f"{P}/I2554/2025-08-13.json"] = {
        "rowCount": 1, "rows": [_row("I2554", "I2537", 8)]}
    s.docs[f"{P}/I2537/2020-07-28.json"] = {
        "rowCount": 1, "rows": [_row("I2537", "I2554", 99)]}
    return s


def test_keys_for_date_selects_only_that_date():
    keys = keys_for_date(_store(), "2025-08-13")
    assert keys == [f"{P}/I2537/2025-08-13.json", f"{P}/I2554/2025-08-13.json"]


def test_fetch_rows_concatenates_across_intersections():
    rows = fetch_rows_for_date(_store(), "2025-08-13")
    assert len(rows) == 3
    assert {r["intersection_id"] for r in rows} == {"I2537", "I2554"}
    assert all(r["survey_date"] == "2025-08-13" for r in rows)


def test_fetch_rows_missing_date_is_empty():
    assert fetch_rows_for_date(_store(), "1999-01-01") == []


def test_counts_fieldnames_preferred_order_then_extras():
    rows = [{"totals": 1, "intersection_id": "I1", "zeta": 9, "destination_id": "I2"}]
    fn = counts_fieldnames(rows)
    # preferred columns come first in canonical order; unknown "zeta" is appended
    assert fn.index("intersection_id") < fn.index("destination_id") < fn.index("totals")
    assert fn[-1] == "zeta"


def test_write_counts_csv_roundtrips(tmp_path):
    rows = fetch_rows_for_date(_store(), "2025-08-13")
    dst = tmp_path / "counts.csv"
    n = write_counts_csv(rows, dst)
    assert n == 3
    back = list(csv.DictReader(dst.open()))
    assert len(back) == 3
    # generator-required columns are present and populated
    assert {"intersection_id", "destination_id", "totals"} <= set(back[0].keys())
    assert back[0]["intersection_id"] == "I2537"


def test_write_counts_csv_empty_writes_header_only(tmp_path):
    dst = tmp_path / "empty.csv"
    n = write_counts_csv([], dst)
    assert n == 0
    lines = dst.read_text().splitlines()
    assert len(lines) == 1  # header only
    assert "intersection_id" in lines[0]


def test_available_dates_counts_intersections_per_date():
    d = available_dates(_store())
    assert d == {"2025-08-13": 2, "2020-07-28": 1}


def test_available_dates_empty_store():
    assert available_dates(MemStore()) == {}


def test_build_summary_reports_coverage(tmp_path):
    dst = tmp_path / "out.csv"
    summ = build_counts_csv_for_date(_store(), "2025-08-13", dst)
    assert summ["rows"] == 3
    assert summ["intersections"] == 2
    assert summ["time_slots"] == ["07:00", "07:15"]
    assert dst.is_file()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
