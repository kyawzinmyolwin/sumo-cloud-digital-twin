"""
Unit tests for the query core — in-memory store, no AWS.

    python -m pytest etl/tests/test_query.py   # or: python etl/tests/test_query.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from etl.query import query_counts  # noqa: E402

P = "processed-traffic-data"


class MemStore:
    def __init__(self):
        self.docs = {}

    def get_json(self, key):
        return self.docs.get(key)

    def list_keys(self, prefix):
        return [k for k in self.docs if k.startswith(prefix)]


def _store():
    s = MemStore()
    s.docs[f"{P}/I0007/2016-08-24.json"] = {"intersection": "I0007", "date": "2016-08-24",
                                            "rowCount": 2, "rows": [{"m": 1}, {"m": 2}]}
    s.docs[f"{P}/I0007/2026-02-24.json"] = {"intersection": "I0007", "date": "2026-02-24",
                                            "rowCount": 1, "rows": [{"m": 9}]}
    s.docs[f"{P}/I0003/2020-02-17.json"] = {"intersection": "I0003", "date": "2020-02-17",
                                            "rowCount": 1, "rows": [{"m": 5}]}
    return s


def test_intersection_and_date_returns_rows():
    r = query_counts(_store(), intersection="I0007", date="2016-08-24")
    assert r["found"] is True
    assert r["rowCount"] == 2
    assert r["rows"] == [{"m": 1}, {"m": 2}]


def test_missing_date_reports_not_found():
    r = query_counts(_store(), intersection="I0007", date="1999-01-01")
    assert r["found"] is False
    assert r["rows"] == []


def test_intersection_only_lists_dates_sorted():
    r = query_counts(_store(), intersection="I0007")
    assert r["dates"] == ["2016-08-24", "2026-02-24"]


def test_discovery_lists_intersections_with_blank_names_by_default():
    r = query_counts(_store())
    assert r["intersections"] == [{"id": "I0003", "name": ""}, {"id": "I0007", "name": ""}]


def test_discovery_includes_names_when_index_present():
    from etl.counts_etl import NAMES_INDEX_KEY
    s = _store()
    s.docs[NAMES_INDEX_KEY] = {"I0007": "Kirk / Miners / West Coast"}
    r = query_counts(s)
    by_id = {d["id"]: d["name"] for d in r["intersections"]}
    assert by_id["I0007"] == "Kirk / Miners / West Coast"
    assert by_id["I0003"] == ""      # not in the index -> blank


def test_fallback_keyed_object_date_parsing():
    # A group that lacked a clean date is stored with a __workbook suffix.
    s = MemStore()
    s.docs[f"{P}/I0009/unknown-date__somefile.json"] = {"rows": []}
    r = query_counts(s, intersection="I0009")
    assert r["dates"] == ["unknown-date"]


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
