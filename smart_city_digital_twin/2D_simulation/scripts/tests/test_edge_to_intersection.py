"""Unit tests for the pure helpers in edge_to_intersection.py (no net / CSV files)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edge_to_intersection import build_reverse, normalize_edge, parse_candidates  # noqa: E402


def test_normalize_edge_strips_single_leading_minus():
    assert normalize_edge("-139811056#1") == "139811056#1"
    assert normalize_edge("139811056#1") == "139811056#1"
    assert normalize_edge("  -4830350 ") == "4830350"
    assert normalize_edge("4830350") == "4830350"


def test_parse_candidates():
    assert parse_candidates("a:0.47m;b:3.24m") == [("a", 0.47), ("b", 3.24)]
    assert parse_candidates("") == []
    assert parse_candidates("a;b:1m") == [("b", 1.0)]      # malformed part skipped
    assert parse_candidates("c:2") == [("c", 2.0)]          # 'm' suffix optional


def test_build_reverse_inverts_and_normalizes_sign():
    rows = [{
        "intersection_id": "I1", "dist_m": "0.5",
        "nearest_edge": "139811056#1", "enter_edge": "646361141", "exit_edge": "139811056#1",
        "candidates": "139811056#1:0.47m;24721880#1:7.49m",
    }]
    rev = build_reverse(rows)
    # a query on the reverse direction '-139811056#1' resolves to the same key
    assert normalize_edge("-139811056#1") in rev
    hits = rev["139811056#1"]
    assert hits[0]["intersection"] == "I1"
    # dedup per (edge, intersection): kept the closest distance seen (candidate 0.47 < nearest 0.5)
    assert hits[0]["dist_m"] == 0.47
    assert "646361141" in rev and "24721880#1" in rev


def test_build_reverse_nearest_first_across_intersections():
    rows = [
        {"intersection_id": "I_far", "dist_m": "", "nearest_edge": "", "enter_edge": "",
         "exit_edge": "", "candidates": "E9:5.0m"},
        {"intersection_id": "I_near", "dist_m": "", "nearest_edge": "", "enter_edge": "",
         "exit_edge": "", "candidates": "E9:0.2m"},
    ]
    rev = build_reverse(rows)
    assert [h["intersection"] for h in rev["E9"]] == ["I_near", "I_far"]


def test_build_reverse_ignores_blank_edges():
    rows = [{"intersection_id": "I1", "dist_m": "1.0", "nearest_edge": "",
             "enter_edge": "", "exit_edge": "", "candidates": ""}]
    assert build_reverse(rows) == {}
