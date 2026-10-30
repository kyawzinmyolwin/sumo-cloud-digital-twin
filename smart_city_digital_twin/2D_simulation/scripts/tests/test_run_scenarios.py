"""Unit tests for the pure helpers in run_scenarios.py (no SUMO / InfluxDB / subprocess)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_scenarios import (  # noqa: E402
    DEFAULT_SCENARIOS,
    maybe_fast,
    scenarios_to_run,
)
from control_server import build_command  # noqa: E402


def test_default_is_the_compare_pair():
    assert scenarios_to_run(None) == DEFAULT_SCENARIOS == ["baseline_am", "crash_arterial"]


def test_explicit_single_scenario_preserved():
    assert scenarios_to_run(["baseline_am"]) == ["baseline_am"]
    # order is preserved as given
    assert scenarios_to_run(["crash_arterial", "baseline_am"]) == ["crash_arterial", "baseline_am"]


def test_scenarios_to_run_copies_input():
    req = ["baseline_am"]
    out = scenarios_to_run(req)
    out.append("x")
    assert req == ["baseline_am"]  # caller's list not mutated


def test_maybe_fast_strips_realtime_only_when_fast():
    cmd = build_command("crash_arterial", python="PY")
    assert "--real-time" in cmd
    assert maybe_fast(cmd, False) == cmd                 # untouched
    fast = maybe_fast(cmd, True)
    assert "--real-time" not in fast
    # the scenario tag and the incident must survive the strip
    assert "crash_arterial" in fast
    assert "--close-edge" in fast


def test_maybe_fast_does_not_mutate_input():
    cmd = build_command("baseline_am", python="PY")
    maybe_fast(cmd, True)
    assert "--real-time" in cmd  # original list unchanged
