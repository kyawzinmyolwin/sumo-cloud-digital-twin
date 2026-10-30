"""Unit tests for the pure aggregation/delta helpers in capture_results.py (no network)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pytest import approx  # noqa: E402

from capture_results import build_table, delta, pct_drop, summarise  # noqa: E402


def test_summarise_mean_max_count():
    s = summarise([{"time": "t1", "value": 2.0}, {"time": "t2", "value": 4.0}, {"time": "t3", "value": 6.0}])
    assert s["n"] == 3
    assert s["mean"] == 4.0
    assert s["max"] == 6.0


def test_summarise_empty_and_nonnumeric():
    assert summarise([]) == {"mean": None, "max": None, "n": 0}
    assert summarise([{"time": "t", "value": None}, {"time": "t", "value": "x"}])["n"] == 0


def test_pct_drop():
    assert pct_drop(10.0, 7.5) == 25.0        # 25% slower
    assert pct_drop(10.0, 12.0) == -20.0      # faster -> negative drop
    assert pct_drop(0.0, 5.0) is None         # guard against divide-by-zero
    assert pct_drop(None, 5.0) is None


def test_delta():
    assert delta(0.30, 0.55) == approx(0.25)
    assert delta(0.55, 0.30) == approx(-0.25)
    assert delta(None, 1.0) is None


def test_build_table_shape_and_baseline_dashes():
    data = {
        "baseline_am": {
            "avgSpeed": {"mean": 10.0, "max": 14.0, "n": 5},
            "congestionIndex": {"mean": 0.30, "max": 0.5, "n": 5},
            "stoppedCount": {"mean": 12.0, "max": 40.0, "n": 5},
            "vehicleCount": {"mean": 200.0, "max": 260.0, "n": 5},
        },
        "crash_arterial": {
            "avgSpeed": {"mean": 7.5, "max": 12.0, "n": 5},
            "congestionIndex": {"mean": 0.55, "max": 0.8, "n": 5},
            "stoppedCount": {"mean": 30.0, "max": 90.0, "n": 5},
            "vehicleCount": {"mean": 210.0, "max": 280.0, "n": 5},
        },
    }
    table = build_table(["baseline_am", "crash_arterial"], data)
    lines = table.splitlines()
    assert lines[0].startswith("| Scenario |")
    # baseline row has em-dash deltas
    base_row = next(l for l in lines if "`baseline_am`" in l)
    assert "| — | " in base_row
    # incident row: 10 m/s -> 36 km/h baseline, 7.5 -> 27 km/h => -25.0% slower; congestion +0.25
    crash_row = next(l for l in lines if "`crash_arterial`" in l)
    assert "-25.0%" in crash_row
    assert "+0.25" in crash_row
