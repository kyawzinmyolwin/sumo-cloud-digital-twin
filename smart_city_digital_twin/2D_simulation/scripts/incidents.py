"""
Incident injection for the scenario / decision-support twin (Increment 1).

Perturbs the *running* SUMO simulation via TraCI so we can ask "what if?" — a
crash or road closure, a roadworks speed limit, or an event demand surge — and
watch the impact stream to the dashboard like any other run. The perturbation
rides on the calibrated street-net model; nothing here re-generates demand.

Design (matches the repo's pure-core / thin-glue split):
- The *spec* is pure data and fully unit-testable without SUMO: an ``Incident``
  dataclass, ``parse_incidents`` (from a JSON dict), ``parse_cli_incident`` (from
  a "edge@start[:end]" string), and ``active_at``.
- The *application* is the only TraCI-touching part: ``IncidentController.step``
  applies an incident when it becomes active and reverts it when it expires,
  restoring the exact original state it saved. The traci module is passed in, so
  tests drive it with a fake.

Supported types (Increment 1):
- ``close_edge`` / ``close_lane``  — block the road (max speed -> ~0), models a
  crash. Vehicles queue upstream; congestion builds and is visible on the map.
- ``set_speed``                    — lower the max speed on an edge/lane (roadworks
  / temporary limit). ``close_*`` is just this with a near-zero speed.
- ``add_vehicles``                 — inject extra trips from one edge to another
  across the window (event surge). Best-effort routing via findRoute.

All closures/speed changes are reverted to the saved original on expiry, so a run
can contain several timed incidents without leaking state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Near-zero speed used to model a full block (SUMO dislikes exactly 0).
BLOCK_SPEED = 0.1


@dataclass
class Incident:
    """One timed perturbation. ``target`` is an edge id (close_edge/set_speed/
    add_vehicles source) or a lane id (close_lane); ``to_target`` is the
    add_vehicles destination edge."""

    type: str                       # close_edge | close_lane | set_speed | add_vehicles
    target: str = ""
    start: float = 0.0
    end: float | None = None        # None = for the rest of the run
    speed: float | None = None      # set_speed: the max speed to impose (m/s)
    to_target: str = ""             # add_vehicles: destination edge
    count: int = 0                  # add_vehicles: how many to inject over the window
    vtype: str = "passenger"        # add_vehicles: vType id
    label: str = ""                 # optional human label for logs

    def describe(self) -> str:
        when = f"{self.start:.0f}" + (f"-{self.end:.0f}" if self.end is not None else "+")
        if self.type == "add_vehicles":
            return f"[{when}] surge {self.count} {self.target}->{self.to_target}"
        if self.type == "set_speed":
            return f"[{when}] speed {self.target} -> {self.speed} m/s"
        return f"[{when}] {self.type} {self.target}"


def active_at(inc: Incident, t: float) -> bool:
    """True while ``t`` is in the half-open window [start, end) (end None = open)."""
    return t >= inc.start and (inc.end is None or t < inc.end)


def parse_incidents(data: dict[str, Any]) -> list[Incident]:
    """Build incidents from a spec dict: ``{"incidents": [ {..}, .. ]}``.

    Each entry needs a ``type``; missing optional fields take the dataclass
    defaults. Raises ValueError on an unknown type or a missing required target
    so a bad spec fails loudly rather than silently doing nothing.
    """
    known = {"close_edge", "close_lane", "set_speed", "add_vehicles"}
    out: list[Incident] = []
    for i, raw in enumerate(data.get("incidents", []) or []):
        typ = str(raw.get("type", "")).strip()
        if typ not in known:
            raise ValueError(f"incident #{i}: unknown type {typ!r} (expected one of {sorted(known)})")
        inc = Incident(
            type=typ,
            target=str(raw.get("target", raw.get("edge", raw.get("lane", "")))),
            start=float(raw.get("start", 0.0)),
            end=(None if raw.get("end") in (None, "") else float(raw["end"])),
            speed=(None if raw.get("speed") in (None, "") else float(raw["speed"])),
            to_target=str(raw.get("to", raw.get("to_edge", ""))),
            count=int(raw.get("count", 0) or 0),
            vtype=str(raw.get("vtype", "passenger")),
            label=str(raw.get("label", "")),
        )
        if typ == "set_speed" and inc.speed is None:
            raise ValueError(f"incident #{i}: set_speed needs a 'speed'")
        if typ == "add_vehicles" and (not inc.target or not inc.to_target or inc.count <= 0):
            raise ValueError(f"incident #{i}: add_vehicles needs 'target'(from), 'to', and count>0")
        if typ != "add_vehicles" and not inc.target:
            raise ValueError(f"incident #{i}: {typ} needs a target edge/lane")
        out.append(inc)
    return out


def incident_from_control(cmd: dict[str, Any], now: float) -> Incident:
    """Build a live incident from a dashboard control command at sim time ``now``.

    ``cmd`` e.g. ``{"action":"incident","type":"close_edge","edge":"4891423","duration":600}``.
    start = now (apply immediately); end = now+duration, or None (until run end) when no
    duration. Supports the closure/speed family (add_vehicles isn't a live click action).
    """
    typ = str(cmd.get("type", "close_edge"))
    if typ not in {"close_edge", "close_lane", "set_speed"}:
        raise ValueError(f"unsupported live incident type {typ!r}")
    target = str(cmd.get("edge", cmd.get("lane", cmd.get("target", "") or "")))
    if not target:
        raise ValueError("live incident needs an edge/lane target")
    dur = cmd.get("duration")
    end = now + float(dur) if dur not in (None, "", 0, "0") else None
    speed = cmd.get("speed")
    if typ == "set_speed" and speed is None:
        raise ValueError("live set_speed needs a 'speed'")
    return Incident(type=typ, target=target, start=float(now), end=end,
                    speed=(float(speed) if speed is not None else None), label="live")


def parse_cli_incident(s: str) -> Incident:
    """Parse a quick CLI closure: ``EDGE@START[:END]`` -> a close_edge incident.

    e.g. ``E123@23460:23760`` closes edge E123 from t=23460 to t=23760;
    ``E123@23460`` closes it from 23460 for the rest of the run.
    """
    if "@" not in s:
        raise ValueError(f"--close-edge must be EDGE@START[:END], got {s!r}")
    edge, when = s.split("@", 1)
    if ":" in when:
        a, b = when.split(":", 1)
        start, end = float(a), float(b)
    else:
        start, end = float(when), None
    if not edge:
        raise ValueError(f"--close-edge missing edge id in {s!r}")
    return Incident(type="close_edge", target=edge.strip(), start=start, end=end)


class IncidentController:
    """Applies/reverts incidents against a live TraCI connection.

    Call ``step(traci, t)`` once per simulation step. It applies each incident as
    it becomes active (saving the state it changes) and reverts it when it expires
    (restoring that state). ``add_vehicles`` injects gradually across its window.
    """

    def add(self, incident: Incident) -> None:
        """Append an incident at runtime (live injection). step() picks it up on the
        next tick by its new index — safe because applied-state is keyed by index."""
        self._incidents.append(incident)

    def reopen_all(self, now: float) -> int:
        """Lift every still-active incident without stopping the sim (the live 'Reopen').

        Sets each active incident's end to ``now`` so the next ``step`` reverts it
        (restoring the lane / speed). Returns how many were lifted. Traffic then
        recovers on its own as the queue drains — we don't force anything."""
        n = 0
        for inc in self._incidents:
            if inc.end is None or inc.end > now:
                inc.end = now
                n += 1
        return n

    def __init__(self, incidents: list[Incident], *, log=print) -> None:
        self._incidents = list(incidents)
        self._applied: set[int] = set()          # indices of currently-applied incidents
        self._saved_speed: dict[int, dict[str, float]] = {}  # idx -> {lane_id: original speed}
        self._injected: dict[int, int] = {}       # add_vehicles idx -> vehicles injected so far
        self._veh_seq = 0
        self._log = log

    # --- lane helpers (kept tiny so a fake traci can stand in) ----------------
    @staticmethod
    def _lanes_of(traci, target: str) -> list[str]:
        """Lane ids for an edge target, or [target] if it's already a lane id."""
        all_lanes = traci.lane.getIDList()
        if target in all_lanes:                    # already a lane id
            return [target]
        return [lid for lid in all_lanes if lid.rsplit("_", 1)[0] == target]

    def _apply_speed(self, traci, idx: int, inc: Incident, speed: float) -> None:
        saved: dict[str, float] = {}
        for lid in self._lanes_of(traci, inc.target):
            saved[lid] = traci.lane.getMaxSpeed(lid)
            traci.lane.setMaxSpeed(lid, speed)
        self._saved_speed[idx] = saved
        self._log(f"incident ON  {inc.describe()} ({len(saved)} lane(s))")

    def _revert_speed(self, traci, idx: int, inc: Incident) -> None:
        for lid, orig in self._saved_speed.get(idx, {}).items():
            traci.lane.setMaxSpeed(lid, orig)
        self._saved_speed.pop(idx, None)
        self._log(f"incident OFF {inc.describe()} (restored)")

    def _inject_surge(self, traci, idx: int, inc: Incident, t: float) -> None:
        """Inject add_vehicles gradually so ``count`` are added across the window."""
        # target injected-so-far = count * elapsed_fraction (ramps 0..count)
        if inc.end and inc.end > inc.start:
            frac = min(1.0, max(0.0, (t - inc.start) / (inc.end - inc.start)))
        else:
            frac = 1.0                              # no window -> inject all at once
        want = int(round(inc.count * frac))
        have = self._injected.get(idx, 0)
        if want <= have:
            return
        route_id = f"inc{idx}_route"
        if have == 0:                               # first injection: build the route once
            try:
                edges = traci.simulation.findRoute(inc.target, inc.to_target).edges
            except Exception as exc:                # noqa: BLE001
                self._log(f"incident surge {inc.label or idx}: findRoute failed ({exc}); skipping")
                self._injected[idx] = inc.count     # don't retry every tick
                return
            if not edges:
                self._log(f"incident surge {inc.label or idx}: no route "
                          f"{inc.target}->{inc.to_target}; skipping")
                self._injected[idx] = inc.count
                return
            traci.route.add(route_id, edges)
        for _ in range(want - have):
            self._veh_seq += 1
            traci.vehicle.add(f"inc{idx}_v{self._veh_seq}", route_id, typeID=inc.vtype)
        self._injected[idx] = want

    # --- the per-step driver --------------------------------------------------
    def step(self, traci, t: float) -> None:
        for idx, inc in enumerate(self._incidents):
            on = active_at(inc, t)
            if inc.type == "add_vehicles":
                if on:
                    self._inject_surge(traci, idx, inc, t)
                continue
            if on and idx not in self._applied:
                self._apply_speed(traci, idx, inc,
                                  inc.speed if inc.type == "set_speed" else BLOCK_SPEED)
                self._applied.add(idx)
            elif not on and idx in self._applied:
                self._revert_speed(traci, idx, inc)
                self._applied.discard(idx)

    @property
    def summary(self) -> str:
        return "; ".join(i.describe() for i in self._incidents) or "(none)"
