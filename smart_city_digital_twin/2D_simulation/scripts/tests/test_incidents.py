"""
Unit tests for the incident-injection core (Increment 1) — no SUMO/TraCI.

The spec parsing and active_at are pure. IncidentController is exercised with a
fake traci that records setMaxSpeed calls and serves a tiny lane list, so the
apply/revert lifecycle is tested without a running simulation.

    python -m pytest tests/test_incidents.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from incidents import (  # noqa: E402
    BLOCK_SPEED,
    Incident,
    IncidentController,
    active_at,
    incident_from_control,
    parse_cli_incident,
    parse_incidents,
)


# --- pure spec ---------------------------------------------------------------
def test_active_at_half_open_window():
    inc = Incident(type="close_edge", target="E1", start=100, end=200)
    assert not active_at(inc, 99)
    assert active_at(inc, 100)
    assert active_at(inc, 199)
    assert not active_at(inc, 200)      # end is exclusive


def test_active_at_open_ended():
    inc = Incident(type="close_edge", target="E1", start=100, end=None)
    assert not active_at(inc, 50)
    assert active_at(inc, 10_000)


def test_parse_incidents_all_types():
    spec = {"incidents": [
        {"type": "close_edge", "edge": "E1", "start": 100, "end": 200},
        {"type": "set_speed", "edge": "E2", "speed": 5, "start": 0},
        {"type": "close_lane", "lane": "E3_0", "start": 10},
        {"type": "add_vehicles", "target": "E4", "to": "E5", "count": 20, "start": 0, "end": 600},
    ]}
    incs = parse_incidents(spec)
    assert [i.type for i in incs] == ["close_edge", "set_speed", "close_lane", "add_vehicles"]
    assert incs[0].target == "E1" and incs[0].end == 200
    assert incs[1].speed == 5
    assert incs[3].to_target == "E5" and incs[3].count == 20


def test_parse_incidents_rejects_unknown_and_incomplete():
    with pytest.raises(ValueError):
        parse_incidents({"incidents": [{"type": "explode", "edge": "E1"}]})
    with pytest.raises(ValueError):
        parse_incidents({"incidents": [{"type": "set_speed", "edge": "E1"}]})  # no speed
    with pytest.raises(ValueError):
        parse_incidents({"incidents": [{"type": "add_vehicles", "target": "E1", "count": 0}]})


def test_parse_cli_incident_with_and_without_end():
    a = parse_cli_incident("E123@23460:23760")
    assert a.type == "close_edge" and a.target == "E123" and a.start == 23460 and a.end == 23760
    b = parse_cli_incident("E9@100")
    assert b.end is None and b.start == 100
    with pytest.raises(ValueError):
        parse_cli_incident("no-at-sign")


# --- controller lifecycle with a fake traci ----------------------------------
class FakeLane:
    def __init__(self, ids, speed=13.9):
        self._ids = ids
        self.speed = {lid: speed for lid in ids}
        self.calls = []

    def getIDList(self):
        return list(self._ids)

    def getMaxSpeed(self, lid):
        return self.speed[lid]

    def setMaxSpeed(self, lid, v):
        self.speed[lid] = v
        self.calls.append((lid, v))


class FakeTraci:
    def __init__(self, lane_ids):
        self.lane = FakeLane(lane_ids)


def test_close_edge_applies_then_reverts():
    tc = FakeTraci(["E1_0", "E1_1", "E2_0"])
    ctrl = IncidentController([Incident(type="close_edge", target="E1", start=100, end=200)],
                             log=lambda *a: None)

    ctrl.step(tc, 50)                    # before -> nothing changes
    assert tc.lane.speed["E1_0"] == pytest.approx(13.9)

    ctrl.step(tc, 100)                   # active -> both E1 lanes blocked, E2 untouched
    assert tc.lane.speed["E1_0"] == BLOCK_SPEED
    assert tc.lane.speed["E1_1"] == BLOCK_SPEED
    assert tc.lane.speed["E2_0"] == pytest.approx(13.9)

    ctrl.step(tc, 200)                   # expired -> restored to original
    assert tc.lane.speed["E1_0"] == pytest.approx(13.9)
    assert tc.lane.speed["E1_1"] == pytest.approx(13.9)


def test_set_speed_uses_given_speed_and_restores():
    tc = FakeTraci(["E1_0"])
    ctrl = IncidentController([Incident(type="set_speed", target="E1", speed=4.0, start=0, end=10)],
                             log=lambda *a: None)
    ctrl.step(tc, 0)
    assert tc.lane.speed["E1_0"] == 4.0
    ctrl.step(tc, 10)
    assert tc.lane.speed["E1_0"] == pytest.approx(13.9)


def test_close_lane_only_touches_that_lane():
    tc = FakeTraci(["E1_0", "E1_1"])
    ctrl = IncidentController([Incident(type="close_lane", target="E1_0", start=0)],
                             log=lambda *a: None)
    ctrl.step(tc, 0)
    assert tc.lane.speed["E1_0"] == BLOCK_SPEED
    assert tc.lane.speed["E1_1"] == pytest.approx(13.9)   # sibling lane stays open


def test_apply_is_idempotent_across_steps():
    tc = FakeTraci(["E1_0"])
    ctrl = IncidentController([Incident(type="close_edge", target="E1", start=0, end=100)],
                             log=lambda *a: None)
    ctrl.step(tc, 0)
    ctrl.step(tc, 1)
    ctrl.step(tc, 2)
    # only one setMaxSpeed call to block (no re-apply, no lost original)
    assert tc.lane.calls == [("E1_0", BLOCK_SPEED)]
    ctrl.step(tc, 100)
    assert tc.lane.speed["E1_0"] == pytest.approx(13.9)


def test_incident_from_control_close_edge_with_duration():
    inc = incident_from_control({"action": "incident", "type": "close_edge",
                                 "edge": "4891423", "duration": 600}, now=23400.0)
    assert inc.type == "close_edge" and inc.target == "4891423"
    assert inc.start == 23400.0 and inc.end == 24000.0 and inc.label == "live"


def test_incident_from_control_no_duration_is_open_ended():
    inc = incident_from_control({"type": "close_edge", "edge": "E1"}, now=100.0)
    assert inc.start == 100.0 and inc.end is None


def test_incident_from_control_rejects_bad_input():
    import pytest as _pt
    with _pt.raises(ValueError):
        incident_from_control({"type": "add_vehicles", "edge": "E1"}, now=0)   # not a live type
    with _pt.raises(ValueError):
        incident_from_control({"type": "close_edge"}, now=0)                   # no target
    with _pt.raises(ValueError):
        incident_from_control({"type": "set_speed", "edge": "E1"}, now=0)      # no speed


def test_controller_add_injects_at_runtime():
    tc = FakeTraci(["E1_0"])
    ctrl = IncidentController([], log=lambda *a: None)
    ctrl.step(tc, 100)                                   # nothing scheduled yet
    assert tc.lane.speed["E1_0"] == pytest.approx(13.9)
    ctrl.add(incident_from_control({"type": "close_edge", "edge": "E1"}, now=100))
    ctrl.step(tc, 101)                                   # live incident now applies
    assert tc.lane.speed["E1_0"] == BLOCK_SPEED


def test_reopen_all_lifts_live_block():
    tc = FakeTraci(["E1_0"])
    ctrl = IncidentController([], log=lambda *a: None)
    ctrl.add(incident_from_control({"type": "close_edge", "edge": "E1"}, now=100))  # open-ended
    ctrl.step(tc, 101)
    assert tc.lane.speed["E1_0"] == BLOCK_SPEED          # blocked

    n = ctrl.reopen_all(now=200)                         # the live "Reopen"
    assert n == 1
    ctrl.step(tc, 201)                                   # next tick reverts it
    assert tc.lane.speed["E1_0"] == pytest.approx(13.9)  # road restored, sim still running

    n2 = ctrl.reopen_all(now=300)                        # nothing left to lift
    assert n2 == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
