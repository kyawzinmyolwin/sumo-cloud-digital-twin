"""
Unit tests for counts_etl — no openpyxl, no real parser, no network.

clean_workbook takes an injectable parse_xlsx, so we feed it fakes to exercise
the three outcomes (rows / raises / empty) and the JSON shaping.

    python -m pytest etl/tests/test_counts_etl.py   # or: python etl/tests/test_counts_etl.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from etl.counts_etl import (  # noqa: E402
    clean_workbook,
    group_by_intersection_date,
    intersection_name_from_filename,
    to_json_bytes,
)


def _rows_parser(rows):
    return lambda path: rows


def _raising_parser(exc):
    def _fn(path):
        raise exc
    return _fn


def test_ok_when_parser_returns_rows():
    rows = [{"intersection_id": "I0003", "survey_date": "2020-02-17", "totals": 42}]
    res = clean_workbook("x.xlsx", parse_xlsx=_rows_parser(rows))
    assert res.ok
    assert res.status == "ok"
    assert res.row_count == 1
    assert res.rows == rows
    assert res.reason == ""


def test_rejected_when_parser_raises_valueerror():
    exc = ValueError("Expected at least one Miovision metric sheet with a movement grid\n(more)")
    res = clean_workbook("ped.xlsx", parse_xlsx=_raising_parser(exc))
    assert not res.ok
    assert res.status == "rejected"
    # reason is the first line only, so it stays log-friendly
    assert res.reason.startswith("Expected at least one Miovision metric sheet")
    assert "\n" not in res.reason


def test_rejected_when_parser_returns_no_rows():
    res = clean_workbook("template.xlsx", parse_xlsx=_rows_parser([]))
    assert res.status == "rejected"
    assert res.reason == "parser returned 0 rows"


def test_rejected_on_any_parser_exception():
    res = clean_workbook("corrupt.xls", parse_xlsx=_raising_parser(RuntimeError("bad zip")))
    assert res.status == "rejected"
    assert res.reason == "bad zip"


def test_to_json_bytes_roundtrips_with_source():
    rows = [{"intersection_id": "I0007", "survey_date": "2026-02-24", "totals": 7}]
    res = clean_workbook("x.xlsx", parse_xlsx=_rows_parser(rows))
    src = {"driveId": "abc", "name": "x.xlsx", "folder": "2026 Intersection"}
    payload = json.loads(to_json_bytes(res, source=src).decode("utf-8"))
    assert payload["rowCount"] == 1
    assert payload["rows"] == rows
    assert payload["source"]["driveId"] == "abc"


def test_group_by_intersection_date():
    rows = [
        {"intersection_id": "I1", "survey_date": "2020-01-01", "v": 1},
        {"intersection_id": "I1", "survey_date": "2020-01-01", "v": 2},
        {"intersection_id": "I2", "survey_date": "2021-05-05", "v": 3},
        {"totals": 9},  # missing id/date -> ("","") group, not dropped
    ]
    groups = group_by_intersection_date(rows)
    assert len(groups[("I1", "2020-01-01")]) == 2
    assert groups[("I2", "2021-05-05")] == [{"intersection_id": "I2", "survey_date": "2021-05-05", "v": 3}]
    assert ("", "") in groups


def test_intersection_name_from_filename():
    f = intersection_name_from_filename
    assert f("I0007_Kirk___Miners___West_Coast_339745_08-24-2016.xls") == "Kirk / Miners / West Coast"
    assert f("I0055_SP_Pound_Roberts_390379_02-22-2017.xls") == "SP Pound Roberts"
    assert f("I0019_Kirk Road_Maddisons Road_Intersection.xlsx") == "Kirk Road Maddisons Road"
    assert f("") == ""


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
