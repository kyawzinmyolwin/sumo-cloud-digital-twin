"""
Unit tests for replay_server's Flux builder + scenario validation (no network).

    python -m pytest tests/test_replay_server.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from replay_server import build_flux, _SCENARIO_RE  # noqa: E402


def test_build_flux_without_scenario_has_no_scenario_filter():
    flux = build_flux("bkt", "-1h", "now()", "avgSpeed", "1m")
    assert "scenario_id" not in flux
    assert '_measurement == "traffic_metrics"' in flux
    assert '_field == "avgSpeed"' in flux
    assert "aggregateWindow(every: 1m" in flux


def test_build_flux_with_scenario_adds_filter():
    flux = build_flux("bkt", "-1h", "now()", "avgSpeed", None, "crash_arterial")
    assert 'r.scenario_id == "crash_arterial"' in flux
    assert "aggregateWindow" not in flux          # every=None -> no window


def test_scenario_regex_accepts_normal_names():
    for ok in ("baseline_am", "crash_arterial", "am-2025", "scn.1", "christchurch-cbd-001"):
        assert _SCENARIO_RE.match(ok)


def test_scenario_regex_rejects_injection_and_junk():
    for bad in ('a" or true', "has space", "drop;table", "x" * 65, "", "a/b"):
        assert not _SCENARIO_RE.match(bad)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
