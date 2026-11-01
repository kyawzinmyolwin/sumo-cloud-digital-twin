"""
Live vehicle-state emitter for the Christchurch CBD SUMO simulation.

Two responsibilities, deliberately kept apart:

1. ``serialize_vehicles`` — pure function. Given a live ``traci`` connection and
   a ``sumolib`` net (only used for XY -> WGS84 conversion), it returns the JSON
   snapshot dict. It imports nothing from SUMO, so it can be unit-tested with
   fakes and no SUMO install (see tests/test_emitter.py).

2. ``Broadcaster`` + ``serve`` — the async WebSocket side. The broadcaster keeps
   the set of connected browsers and the latest snapshot, so a brand-new client
   gets the current state immediately instead of waiting for the next tick.

run_traci.py owns the TraCI stepping loop and calls into here. This module never
touches traci itself except through the object handed to ``serialize_vehicles``.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Iterable

# websockets is only needed for the live server, not for serialization, so the
# import is deferred into serve()/Broadcaster usage. That keeps this module (and
# the unit test) importable without the dependency installed.


def serialize_vehicles(
    traci: Any, net: Any, sim_id: str, scenario_id: str | None = None
) -> dict[str, Any]:
    """Build one JSON snapshot of every vehicle currently on the network.

    Parameters
    ----------
    traci:
        A live TraCI connection (or a fake exposing the same attributes).
    net:
        A sumolib net with ``convertXY2LonLat(x, y)`` -> ``(lon, lat)``. Passed
        in rather than imported so this function stays SUMO-free and testable.
    sim_id:
        Scenario identifier echoed back to clients.
    scenario_id:
        Optional decision-support scenario name (e.g. ``baseline_am`` vs
        ``crash_arterial``) used to tag stored metrics for side-by-side comparison.
        Defaults to ``sim_id`` when not given, so the snapshot always carries one.

    Returns
    -------
    dict matching the schema documented in CLAUDE.md.
    """
    vehicle_ids = traci.vehicle.getIDList()
    vehicles: list[dict[str, Any]] = []
    for vid in vehicle_ids:
        # SUMO works in projected metres (x east, y north). The browser map
        # (Leaflet) wants WGS84 lon/lat, so convert per vehicle.
        x, y = traci.vehicle.getPosition(vid)
        lon, lat = net.convertXY2LonLat(x, y)
        vehicles.append(
            {
                "id": vid,
                "lat": round(lat, 6),
                "lng": round(lon, 6),
                "speed": round(traci.vehicle.getSpeed(vid), 3),
                "lane": traci.vehicle.getLaneID(vid),
                "accel": round(traci.vehicle.getAcceleration(vid), 3),
                "type": traci.vehicle.getTypeID(vid),  # "car" / "bus" — drives marker shape
            }
        )

    return {
        "tick": int(time.time() * 1000),          # wall-clock ms, for the client
        "simId": sim_id,
        "scenarioId": scenario_id if scenario_id is not None else sim_id,
        "simTime": round(traci.simulation.getTime(), 3),
        "vehicleCount": len(vehicles),
        "vehicles": vehicles,
    }


def to_json(snapshot: dict[str, Any]) -> str:
    """Serialise a snapshot to a compact JSON string for the wire."""
    return json.dumps(snapshot, separators=(",", ":"))


class Broadcaster:
    """Tracks connected WebSocket clients and the most recent snapshot.

    The stepping loop calls :meth:`broadcast` each tick. New clients that connect
    between ticks are handed :attr:`latest` on connect so the map is never blank.
    """

    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self.latest: str | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def register(self, ws: Any) -> None:
        self._clients.add(ws)

    def unregister(self, ws: Any) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: str) -> None:
        """Send ``message`` to every connected client; drop ones that error."""
        self.latest = message
        if not self._clients:
            return
        dead: list[Any] = []
        # Iterate a snapshot: ``await ws.send`` yields to the event loop, during
        # which a client can connect/disconnect and mutate self._clients. Iterating
        # the live set would raise "Set changed size during iteration".
        for ws in list(self._clients):
            try:
                await ws.send(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.unregister(ws)


class ControlInbox:
    """A queue of inbound control commands from dashboard clients (e.g. "close this
    edge now"). The WebSocket handler appends parsed commands; the stepping loop
    drains them each tick and applies them to the live IncidentController. Kept
    tiny and dependency-free so it's trivially testable."""

    def __init__(self) -> None:
        self._pending: list[dict[str, Any]] = []

    def add(self, cmd: dict[str, Any]) -> None:
        self._pending.append(cmd)

    def drain(self) -> list[dict[str, Any]]:
        out = self._pending
        self._pending = []
        return out


# Live control actions the stepping loop understands (close a road / lift closures).
CONTROL_ACTIONS = frozenset({"incident", "reopen"})


def control_command(raw: Any) -> "dict[str, Any] | None":
    """Parse one inbound WebSocket frame; return it if it's a live control command
    (``{"action": "incident", ...}`` or ``{"action": "reopen"}``), else None.
    Shared by the local server and the cloud forwarder so both accept the same set."""
    try:
        msg = json.loads(raw)
    except Exception:  # noqa: BLE001 - ignore malformed frames
        return None
    if isinstance(msg, dict) and msg.get("action") in CONTROL_ACTIONS:
        return msg
    return None


def producer_url(url: str) -> str:
    """Tag the outbound cloud connection as the producer (``?role=producer``) so the
    ingest Lambda doesn't echo snapshots back to it and the control Lambda can find
    it to deliver live commands."""
    if "role=producer" in url:
        return url
    return url + ("&" if "?" in url else "?") + "role=producer"


async def serve(broadcaster: Broadcaster, host: str, port: int, inbox: "ControlInbox | None" = None):
    """Start the WebSocket server and return the running server object.

    Each connection: send the current snapshot right away, then read inbound
    frames. Clients normally send nothing; when ``inbox`` is given, control frames are
    queued for the stepping loop: ``{"action": "incident", ...}`` to close a road live
    and ``{"action": "reopen"}`` to lift active closures (the live Reopen toggle). Any
    other frame is ignored.
    """
    import websockets

    async def handler(ws: Any) -> None:
        broadcaster.register(ws)
        try:
            if broadcaster.latest is not None:
                await ws.send(broadcaster.latest)
            async for raw in ws:               # ends when the client disconnects
                if inbox is None:
                    continue
                cmd = control_command(raw)
                if cmd is not None:
                    inbox.add(cmd)
        finally:
            broadcaster.unregister(ws)

    return await websockets.serve(handler, host, port)


def wrap_sendmessage(snapshot: dict[str, Any]) -> str:
    """Wrap a snapshot in the envelope API Gateway routes on.

    The WebSocket API selects a route from ``$request.body.action``, so the top
    level must carry ``action: "sendmessage"``; the snapshot rides under ``data``
    (what the traffic-ingest Lambda reads). Pure + trivially unit-testable.
    """
    return json.dumps({"action": "sendmessage", "data": snapshot}, separators=(",", ":"))


class CloudForwarder:
    """Forwards each snapshot to a WebSocket target (e.g. the API Gateway URL).

    This is the client counterpart to ``serve``: instead of hosting a server for
    browsers, the emitter dials OUT to the cloud endpoint and posts each tick.
    Resilient by design — a dropped connection is logged and retried on the next
    tick rather than crashing the simulation loop.

    With an ``inbox``, a background reader also listens on the same connection for
    live control commands the cloud control Lambda pushes to this producer (close a
    road / reopen) and queues them exactly like the local server does.
    """

    def __init__(self, url: str, inbox: "ControlInbox | None" = None) -> None:
        self.url = producer_url(url)
        self.inbox = inbox
        self._ws: Any = None
        self._reader: "asyncio.Task | None" = None

    async def _ensure(self) -> None:
        if self._ws is None:
            import websockets

            self._ws = await websockets.connect(self.url)
            if self.inbox is not None:
                self._reader = asyncio.create_task(self._read(self._ws))

    async def _read(self, ws: Any) -> None:
        """Queue control commands arriving on ``ws``; ends when the connection drops
        (send() then reconnects on the next tick and starts a new reader)."""
        try:
            async for raw in ws:
                cmd = control_command(raw)
                if cmd is not None and self.inbox is not None:
                    self.inbox.add(cmd)
                    print(f"cloud control received: {cmd.get('action')}")
        except Exception:  # noqa: BLE001 - a dropped connection is handled by send()
            pass

    async def send(self, snapshot: dict[str, Any]) -> None:
        payload = wrap_sendmessage(snapshot)
        try:
            await self._ensure()
            await self._ws.send(payload)
        except Exception as exc:  # noqa: BLE001 — never let a cloud blip stop the sim
            print(f"cloud forward failed ({exc}); will reconnect next tick")
            if self._reader is not None:
                self._reader.cancel()
                self._reader = None
            self._ws = None

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
