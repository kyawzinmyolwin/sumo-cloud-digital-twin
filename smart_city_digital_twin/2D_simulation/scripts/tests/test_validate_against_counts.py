"""Unit tests for the pure scoring/parse helpers in validate_against_counts.py (no SUMO)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pytest import approx  # noqa: E402

from validate_against_counts import (  # noqa: E402
    compare,
    geh,
    observed_by_edge,
    routed_departures_by_edge,
    sim_by_edge,
    summarise,
)


def test_geh_identical_is_zero():
    assert geh(100, 100) == 0.0
    assert geh(0, 0) == 0.0


def test_geh_known_value():
    # M=100, C=121 -> GEH = sqrt(2*441/221) = ~1.998
    assert geh(100, 121) == approx(1.998, abs=1e-2)


def test_geh_symmetric():
    assert geh(80, 100) == approx(geh(100, 80))


def test_compare_joins_and_sorts_by_observed():
    observed = {"a": 50.0, "b": 200.0, "c": 10.0}
    sim = {"a": 55.0, "b": 190.0}  # c missing -> treated as 0
    rows = compare(observed, sim)
    assert [r["edge"] for r in rows] == ["b", "a", "c"]  # sorted desc by observed
    c = next(r for r in rows if r["edge"] == "c")
    assert c["sim"] == 0.0 and c["diff"] == -10.0


def test_compare_min_count_filter():
    rows = compare({"a": 5.0, "b": 100.0}, {"a": 5.0, "b": 100.0}, min_count=10.0)
    assert [r["edge"] for r in rows] == ["b"]


def test_compare_pct_guards_zero_observed():
    rows = compare({"a": 0.0}, {"a": 3.0})
    assert rows[0]["pct"] is None


def test_summarise_quality_metrics():
    rows = compare({"a": 100.0, "b": 100.0}, {"a": 100.0, "b": 40.0})
    s = summarise(rows)
    assert s["n"] == 2
    assert s["total_obs"] == 200.0 and s["total_sim"] == 140.0
    assert s["pct_geh_lt5"] == 50.0   # 'a' GEH 0 (<5), 'b' GEH ~7.3 (>=5)
    assert s["pct_geh_lt10"] == 100.0


def test_summarise_empty():
    assert summarise([])["n"] == 0


def test_observed_by_edge_window_and_sum(tmp_path):
    p = tmp_path / "flows.rou.xml"
    p.write_text(
        '<routes>'
        '<flow id="f1" begin="23400" end="24300" number="8" from="E1" to="X"/>'
        '<flow id="f2" begin="23400" end="24300" number="2" from="E1" to="Y"/>'
        '<flow id="f3" begin="24300" end="25200" number="5" from="E2" to="Z"/>'
        '<flow id="f4" begin="90000" end="90900" number="99" from="E1" to="W"/>'  # out of window
        '</routes>',
        encoding="utf-8",
    )
    obs = observed_by_edge(str(p), 23400, 25200)
    assert obs == {"E1": 10, "E2": 5}  # f4 excluded by window


def test_sim_by_edge_overlapping_intervals(tmp_path):
    p = tmp_path / "edgedata.xml"
    p.write_text(
        '<meandata>'
        '<interval begin="23400" end="25200">'
        '  <edge id="E1" departed="7" entered="40"/>'
        '  <edge id="E2" departed="3" entered="9"/>'
        '</interval>'
        '<interval begin="80000" end="81000">'
        '  <edge id="E1" departed="99" entered="99"/>'  # outside window
        '</interval>'
        '</meandata>',
        encoding="utf-8",
    )
    assert sim_by_edge(str(p), 23400, 27000, "departed") == {"E1": 7.0, "E2": 3.0}
    assert sim_by_edge(str(p), 23400, 27000, "entered") == {"E1": 40.0, "E2": 9.0}


def test_routed_departures_first_edge_in_window(tmp_path):
    p = tmp_path / "routed.rou.xml"
    p.write_text(
        '<routes>\n'
        '<vehicle id="v1" depart="23400.00"><route edges="E1 E9 E8"/></vehicle>\n'
        '<vehicle id="v2" depart="23450.00"><route edges="E1 E2"/></vehicle>\n'
        '<vehicle id="v3" depart="90000.00"><route edges="E1 E2"/></vehicle>\n'  # out of window
        '</routes>\n',
        encoding="utf-8",
    )
    assert routed_departures_by_edge(str(p), 23400, 27000) == {"E1": 2}
