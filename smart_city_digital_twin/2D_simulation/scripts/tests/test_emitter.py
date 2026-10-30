"""
Unit tests for emitter.serialize_vehicles — no SUMO, no websockets required.

The whole point of the emitter's design is that the serialiser depends only on
two objects handed to it (traci, net). Here we hand it fakes and assert the
output matches the schema in CLAUDE.md.

Run from scripts/:  python -m pytest tests/test_emitter.py
Or without pytest:  python tests/test_emitter.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emitter import ControlInbox, serialize_vehicles, to_json, wrap_sendmessage  # noqa: E402


def test_control_inbox_add_and_drain():
    box = ControlInbox()
    assert box.drain() == []
    box.add({"action": "incident", "type": "close_edge", "edge": "E1"})
    box.add({"action": "incident", "type": "close_edge", "edge": "E2"})
    out = box.drain()
    assert [c["edge"] for c in out] == ["E1", "E2"]
    assert box.drain() == []          # drain clears the queue


class FakeVehicleDomain:
    """Stand-in for traci.vehicle, returning canned data for two vehicles."""

    _POS = {"veh_001": (1000.0, 2000.0), "veh_002": (1500.0, 2500.0)}
    _SPEED = {"veh_001": 13.4, "veh_002": 0.0}
    _LANE = {"veh_001": "edge_42_0", "veh_002": "edge_7_1"}
    _ACCEL = {"veh_001": 0.2, "veh_002": -1.5}
    _TYPE = {"veh_001": "car", "veh_002": "bus"}

    def getIDList(self):
        return ("veh_001", "veh_002")

    def getPosition(self, vid):
        return self._POS[vid]

    def getSpeed(self, vid):
        return self._SPEED[vid]

    def getLaneID(self, vid):
        return self._LANE[vid]

    def getAcceleration(self, vid):
        return self._ACCEL[vid]

    def getTypeID(self, vid):
        return self._TYPE[vid]


class FakeSimulationDomain:
    def getTime(self):
        return 23460.1


class FakeTraci:
    def __init__(self):
        self.vehicle = FakeVehicleDomain()
        self.simulation = FakeSimulationDomain()


class FakeNet:
    """Fake sumolib net. Real one projects metres->WGS84; we fake a linear map
    so the test is deterministic and we can assert exact converted values."""

    def convertXY2LonLat(self, x, y):
        # Deliberately simple and reversible so expected values are obvious.
        return (172.0 + x / 100000.0, -43.0 - y / 100000.0)


def test_snapshot_shape_and_conversion():
    snap = serialize_vehicles(FakeTraci(), FakeNet(), sim_id="test-sim")

    assert snap["simId"] == "test-sim"
    assert snap["scenarioId"] == "test-sim"      # defaults to sim_id when not given
    assert snap["simTime"] == 23460.1
    assert snap["vehicleCount"] == 2
    assert isinstance(snap["tick"], int)
    assert len(snap["vehicles"]) == 2

    v1 = snap["vehicles"][0]
    assert v1["id"] == "veh_001"
    # convertXY2LonLat(1000, 2000) -> (172.01, -43.02)
    assert v1["lng"] == 172.01
    assert v1["lat"] == -43.02
    assert v1["speed"] == 13.4
    assert v1["lane"] == "edge_42_0"
    assert v1["accel"] == 0.2
    assert v1["type"] == "car"
    assert snap["vehicles"][1]["type"] == "bus"


def test_scenario_id_overrides_default():
    snap = serialize_vehicles(FakeTraci(), FakeNet(), sim_id="s", scenario_id="crash_arterial")
    assert snap["simId"] == "s"
    assert snap["scenarioId"] == "crash_arterial"


def test_empty_network():
    class EmptyVehicles(FakeVehicleDomain):
        def getIDList(self):
            return ()

    traci = FakeTraci()
    traci.vehicle = EmptyVehicles()
    snap = serialize_vehicles(traci, FakeNet(), sim_id="s")
    assert snap["vehicleCount"] == 0
    assert snap["vehicles"] == []


def test_to_json_roundtrips():
    import json

    snap = serialize_vehicles(FakeTraci(), FakeNet(), sim_id="s")
    assert json.loads(to_json(snap)) == snap


def test_wrap_sendmessage_envelope():
    import json

    snap = serialize_vehicles(FakeTraci(), FakeNet(), sim_id="s")
    wrapped = json.loads(wrap_sendmessage(snap))
    # API Gateway routes on the top-level "action"; the snapshot rides under "data".
    assert wrapped["action"] == "sendmessage"
    assert wrapped["data"] == snap


if __name__ == "__main__":
    # Minimal runner so the file works even without pytest installed.
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
