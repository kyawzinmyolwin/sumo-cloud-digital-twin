"""
Unit tests for live Close/Reopen over the cloud path (no AWS, no SUMO, no network).

Covers the three pieces that make it work:
  - emitter: the producer tags its connection and its background reader queues
    control commands pushed down that connection;
  - traffic-ingest Lambda: connections are recorded with a role, and snapshots fan
    out to viewers only;
  - traffic-control Lambda: /inject and /reopen validate input and report whether a
    live producer received the command.
The Lambda handlers are loaded from infra/functions/ by path, like test_cloud_control.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emitter import CloudForwarder, ControlInbox, control_command, producer_url  # noqa: E402

REPO = Path(__file__).resolve().parents[4]

# traffic-ingest builds its boto3 clients and reads its env at import time.
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("CONNECTIONS_TABLE", "test-connections")
os.environ.setdefault("METRICS_FUNCTION", "test-metrics")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ingest = _load("live_ingest_handler", "infra/functions/traffic_ingest/handler.py")
control = _load("live_control_handler", "infra/functions/traffic_control/handler.py")


# --- emitter -------------------------------------------------------------------
def test_control_command_accepts_only_live_actions():
    assert control_command('{"action":"incident","type":"close_edge","edge":"E1"}')["edge"] == "E1"
    assert control_command('{"action":"reopen"}') == {"action": "reopen"}
    assert control_command('{"tick":1,"vehicles":[]}') is None      # a snapshot, not a command
    assert control_command('{"action":"sendmessage"}') is None
    assert control_command("not json") is None
    assert control_command("[1,2]") is None


def test_producer_url_tags_the_connection_once():
    assert producer_url("wss://x.example/prod") == "wss://x.example/prod?role=producer"
    assert producer_url("wss://x.example/prod?a=1") == "wss://x.example/prod?a=1&role=producer"
    assert producer_url("wss://x.example/prod?role=producer") == "wss://x.example/prod?role=producer"
    assert CloudForwarder("wss://x.example/prod").url.endswith("?role=producer")


class _FakeSocket:
    """Async-iterable stand-in for a websockets connection."""

    def __init__(self, frames):
        self._frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


def test_forwarder_reader_queues_pushed_commands_only():
    box = ControlInbox()
    fwd = CloudForwarder("wss://x.example/prod", inbox=box)
    frames = [
        json.dumps({"action": "incident", "type": "close_edge", "edge": "770109405#0"}),
        json.dumps({"tick": 1, "vehicles": []}),        # ignored
        "garbage",                                       # ignored
        json.dumps({"action": "reopen"}),
    ]
    asyncio.run(fwd._read(_FakeSocket(frames)))
    assert [c["action"] for c in box.drain()] == ["incident", "reopen"]


def test_forwarder_reader_survives_a_dropped_connection():
    class _Dropping(_FakeSocket):
        async def __anext__(self):
            raise ConnectionError("connection closed")

    fwd = CloudForwarder("wss://x.example/prod", inbox=ControlInbox())
    asyncio.run(fwd._read(_Dropping([])))   # must return quietly, not raise


# --- traffic-ingest ------------------------------------------------------------
def test_connection_item_records_role():
    prod = ingest.connection_item("C1", {"role": "producer"}, 1000)
    assert prod["role"] == "producer" and prod["connectionId"] == "C1"
    assert prod["expireAt"] == 1000 + ingest.CONNECTION_TTL_SECONDS
    assert ingest.connection_item("C2", None, 1000)["role"] == "viewer"
    assert ingest.connection_item("C3", {"role": "admin"}, 1000)["role"] == "viewer"


def test_fanout_targets_viewers_only_and_skips_sender():
    items = [
        {"connectionId": "P", "role": "producer"},
        {"connectionId": "V1", "role": "viewer"},
        {"connectionId": "V2"},                     # row from before roles existed
        {"connectionId": "S", "role": "viewer"},
    ]
    assert ingest.fanout_targets(items, skip="S") == ["V1", "V2"]


# --- traffic-control -----------------------------------------------------------
def test_inject_command_validates_edge():
    assert control.inject_command("770109405#0") == {
        "action": "incident", "type": "close_edge", "edge": "770109405#0"}
    assert control.inject_command("-139811056#1", 600)["duration"] == 600
    for bad in ("", "a;rm -rf /", "x" * 81, "edge with space"):
        with pytest.raises(ValueError):
            control.inject_command(bad)


def test_producer_ids_filters_by_role():
    items = [{"connectionId": "P1", "role": "producer"},
             {"connectionId": "V1", "role": "viewer"},
             {"connectionId": "P2", "role": "producer"}]
    assert control.producer_ids(items) == ["P1", "P2"]


def _call(route, q=None):
    resp = control.lambda_handler({"routeKey": route, "queryStringParameters": q}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_inject_route(monkeypatch):
    pushed = []
    monkeypatch.setattr(control, "_push_to_producers", lambda cmd: pushed.append(cmd) or 1)
    status, body = _call("POST /inject", {"edge": "770109405#0"})
    assert status == 200 and body["ok"] and body["producers"] == 1
    assert pushed == [{"action": "incident", "type": "close_edge", "edge": "770109405#0"}]


def test_inject_route_rejects_bad_input_without_pushing(monkeypatch):
    pushed = []
    monkeypatch.setattr(control, "_push_to_producers", lambda cmd: pushed.append(cmd) or 1)
    assert _call("POST /inject", {"edge": "a;b"})[0] == 400
    assert _call("POST /inject", {"edge": "E1", "duration": "soon"})[0] == 400
    assert pushed == []


def test_live_routes_report_when_no_sim_is_connected(monkeypatch):
    monkeypatch.setattr(control, "_push_to_producers", lambda cmd: 0)
    assert _call("POST /inject", {"edge": "E1"})[0] == 409
    assert _call("POST /reopen")[0] == 409


def test_reopen_route(monkeypatch):
    pushed = []
    monkeypatch.setattr(control, "_push_to_producers", lambda cmd: pushed.append(cmd) or 2)
    status, body = _call("POST /reopen")
    assert status == 200 and body["reopened"] and body["producers"] == 2
    assert pushed == [{"action": "reopen"}]
