"""
Run the Christchurch Central City simulation under TraCI control.

Demand in data/output/demand/traffic_trips.routed.rou.xml starts at 06:30 (sim time 23400 s).

Usage:
  # Launch sumo-gui and jump to morning traffic:
  python scripts/run_traci.py --jump-to 23400

  # Connect to sumo-gui you already started with --remote-port:
  sumo-gui -c Christchurch_Central_City_main_streets.sumocfg ^
      --remote-port 8813 --step-length 0.1 --start
  python run_traci.py --connect --jump-to 23400

  # Step in smaller chunks if a large jump crashes SUMO:
  python scripts/run_traci.py --jump-to 23400 --chunk 600
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from _sim_root import SIM_ROOT  # noqa: E402
from emitter import Broadcaster, ControlInbox, CloudForwarder, serialize_vehicles, serve, to_json
from incidents import IncidentController, incident_from_control
from sim_pipeline import SUMOCFG, setup_sumolib, sumo_bin

# Earliest vehicle depart in data/output/demand/traffic_trips.routed.rou.xml (06:30).
DEFAULT_JUMP_TO = 23400.0
DEFAULT_PORT = 8813
DEFAULT_EMIT_PORT = 8765
DEFAULT_SIM_ID = "christchurch-cbd-001"


def _ensure_sumo_tools() -> bool:
    """Make ``traci``/``sumolib`` importable across platforms.

    Tries the repo's Windows-oriented ``setup_sumolib`` first (unchanged). If that
    fails — as it does on macOS/Linux, where the finder looks for netconvert.exe —
    fall back to ``$SUMO_HOME/tools``, which is the correct layout for a Homebrew
    or Linux SUMO install. Purely additive: never changes the Windows path.
    """
    if setup_sumolib() is not None:
        return True
    home = os.environ.get("SUMO_HOME")
    if home:
        tools = Path(home) / "tools"
        if tools.is_dir():
            s = str(tools)
            if s not in sys.path:
                sys.path.insert(0, s)
            return True
    return False


def _import_traci():
    try:
        import traci  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "Could not import traci. Check SUMO_HOME / C:\\Sumo\\tools."
        ) from exc
    return traci


def step_to(traci, target: float, *, chunk: float) -> None:
    """Advance simulation to target time, optionally in chunks."""
    target = float(target)
    if chunk <= 0:
        traci.simulationStep(target)
        _print_status(traci)
        return

    while True:
        current = traci.simulation.getTime()
        if current >= target:
            break
        next_t = min(current + chunk, target)
        traci.simulationStep(next_t)
        _print_status(traci)


def _print_status(traci) -> None:
    t = traci.simulation.getTime()
    n = traci.simulation.getMinExpectedNumber()
    print(f"  sim time {t:8.1f}s ({_fmt_clock(t)}), vehicles on network: {n}")


def _fmt_clock(seconds: float) -> str:
    s = int(seconds) % 86400
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _should_stop(traci, args) -> bool:
    """Shared end-of-run test for both the sync and emitting loops."""
    t = traci.simulation.getTime()
    if args.end is not None and t >= args.end:
        return True
    return traci.simulation.getMinExpectedNumber() <= 0 and t >= (args.jump_to or 0)


def _net_path_for(args) -> Path:
    """The net file the emitter must use for XY -> WGS84 conversion.

    When a --sumocfg is given (a scenario), read that config's <net-file> so the
    conversion matches the network SUMO is actually running — otherwise a scenario
    on the intersection graph would be converted with the default street net's
    projection/offset and land vehicles in the wrong place on the map. Falls back
    to the project default net when there's no --sumocfg or it can't be parsed.
    """
    from sim_pipeline import NET_XML

    cfg = getattr(args, "sumocfg", None)
    if cfg:
        try:
            import xml.etree.ElementTree as ET

            cfg_path = Path(cfg)
            node = ET.parse(cfg_path).getroot().find(".//net-file")
            value = node.get("value") if node is not None else None
            if value:
                return (cfg_path.parent / value).resolve()
        except Exception as exc:  # noqa: BLE001 - fall back to the default net, but say why
            print(f"warning: could not read net-file from {cfg} ({exc}); "
                  f"using default net for coordinate conversion", file=sys.stderr)
    return Path(NET_XML)


def _load_net(args=None):
    """Load the sumolib net once, for XY -> WGS84 conversion in the emitter."""
    import sumolib  # type: ignore

    return sumolib.net.readNet(str(_net_path_for(args)))


async def _run_emitting(traci, args, controller=None) -> None:
    """Step the sim and broadcast each Nth step's snapshot over WebSocket.

    This is the async twin of the plain while-loop in main(). The key line is the
    ``await asyncio.sleep(0)`` at the bottom: it hands control back to the event
    loop so the WebSocket server can accept connections and flush frames before
    we take the next (blocking) simulation step.
    """
    net = _load_net(args)

    # Two independent sinks: a local WebSocket server (--emit, for the browser
    # dashboard) and/or a cloud forwarder (--emit-target, dials out to API Gateway).
    broadcaster = None
    server = None
    forwarder = None
    inbox = None
    if args.emit:
        broadcaster = Broadcaster()
        inbox = ControlInbox()            # dashboard → here: live click-to-inject commands
        server = await serve(broadcaster, args.emit_host, args.emit_port, inbox)
        print(f"Emitter live on ws://{args.emit_host}:{args.emit_port} (sim id: {args.sim_id})")
    if args.emit_target:
        forwarder = CloudForwarder(args.emit_target)
        print(f"Forwarding snapshots to {args.emit_target}")

    step = 0
    # Real-time pacing: anchor wall-clock to sim time so playback tracks the clock
    # (or a --speed multiple of it) instead of running flat out. A light scenario
    # (few vehicles) otherwise finishes in a second; this makes it watchable.
    speed = args.speed if args.speed and args.speed > 0 else 1.0
    sim_start = traci.simulation.getTime()
    wall_start = time.monotonic()
    try:
        print("Running simulation ...")
        while not _should_stop(traci, args):
            t = traci.simulation.getTime()
            # Apply any live control commands that arrived over the WebSocket.
            if inbox is not None:
                for cmd in inbox.drain():
                    if cmd.get("action") == "reopen":
                        n = controller.reopen_all(t) if controller is not None else 0
                        print(f"live reopen: lifted {n} active incident(s)")
                        continue
                    try:
                        inc = incident_from_control(cmd, t)
                    except Exception as exc:  # noqa: BLE001 - bad command shouldn't crash the sim
                        print(f"ignoring live incident command {cmd!r}: {exc}", file=sys.stderr)
                        continue
                    if controller is None:
                        controller = IncidentController([])
                    controller.add(inc)
                    print(f"live incident injected: {inc.describe()}")
            if controller is not None:
                controller.step(traci, t)
            traci.simulationStep()
            step += 1
            # Serialise once per emitted tick, only if a sink actually needs it
            # (a local client is connected, or we're forwarding to the cloud).
            local_wants = broadcaster is not None and broadcaster.client_count
            if step % args.emit_interval == 0 and (local_wants or forwarder is not None):
                snapshot = serialize_vehicles(traci, net, args.sim_id, args.scenario_id)
                if local_wants:
                    await broadcaster.broadcast(to_json(snapshot))
                if forwarder is not None:
                    await forwarder.send(snapshot)
            if int(t) % 60 == 0:
                _print_status(traci)
            # Pace to wall-clock when asked; otherwise just yield to the event loop
            # so the WebSocket server / forwarder can make progress.
            lag = 0.0
            if args.real_time:
                sim_elapsed = traci.simulation.getTime() - sim_start
                lag = (sim_elapsed / speed) - (time.monotonic() - wall_start)
            await asyncio.sleep(lag if lag > 0 else 0)
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        if forwarder is not None:
            await forwarder.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Control SUMO via TraCI for this project.")
    p.add_argument(
        "--connect",
        action="store_true",
        help="Connect to an already running sumo-gui (--remote-port), do not launch SUMO",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"TraCI port (default: {DEFAULT_PORT})",
    )
    p.add_argument("--begin", type=float, default=0.0, help="Simulation begin time (launch mode)")
    p.add_argument(
        "--jump-to",
        type=float,
        default=DEFAULT_JUMP_TO,
        metavar="SEC",
        help=f"Advance to this sim time before the main loop (default: {int(DEFAULT_JUMP_TO)} = 06:30)",
    )
    p.add_argument(
        "--chunk",
        type=float,
        default=3600.0,
        metavar="SEC",
        help="Step in chunks of SEC when jumping (0 = one step; default: 3600)",
    )
    p.add_argument(
        "--step-length",
        type=float,
        default=0.1,
        help="SUMO step length in seconds (launch mode; default: 0.1)",
    )
    p.add_argument("--delay", type=int, default=100, help="sumo-gui delay ms (launch mode)")
    p.add_argument("--end", type=float, default=None, help="Stop at this sim time (default: run until idle)")
    p.add_argument("--gui", action="store_true", default=True, help="Use sumo-gui (default)")
    p.add_argument("--no-gui", action="store_false", dest="gui", help="Use headless sumo instead")
    p.add_argument(
        "--sumocfg",
        default=None,
        metavar="PATH",
        help="Override the SUMO config to launch (launch mode only; ignored with "
        "--connect). Use e.g. scenarios/low_traffic.sumocfg for a small test run.",
    )
    p.add_argument(
        "--real-time",
        action="store_true",
        help="Pace the emitting loop to wall-clock time so a light scenario is "
        "watchable (otherwise it runs as fast as possible). Emit mode only.",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        metavar="X",
        help="Playback multiplier for --real-time (2 = twice real time; default: 1).",
    )
    # --- live emitter (Phase 1) ---
    p.add_argument(
        "--emit",
        action="store_true",
        help="Broadcast per-step vehicle state as JSON over WebSocket (see emitter.py)",
    )
    p.add_argument(
        "--emit-host",
        default="localhost",
        help="WebSocket bind host (default: localhost; use 0.0.0.0 to expose)",
    )
    p.add_argument(
        "--emit-port",
        type=int,
        default=DEFAULT_EMIT_PORT,
        help=f"WebSocket port (default: {DEFAULT_EMIT_PORT})",
    )
    p.add_argument(
        "--emit-interval",
        type=int,
        default=1,
        metavar="N",
        help="Emit every Nth simulation step (default: 1 = every step)",
    )
    p.add_argument(
        "--sim-id",
        default=DEFAULT_SIM_ID,
        help=f"Scenario id echoed to clients (default: {DEFAULT_SIM_ID})",
    )
    p.add_argument(
        "--scenario-id",
        default=None,
        help="Decision-support scenario name tagged onto stored metrics for side-by-side "
        "comparison (e.g. baseline_am vs crash_arterial). Defaults to --sim-id.",
    )
    p.add_argument(
        "--emit-target",
        default=None,
        metavar="WSS_URL",
        help="Also forward each snapshot to this WebSocket URL as a 'sendmessage' "
        "action (e.g. the cloud API Gateway wss:// URL). Can be used with or "
        "without --emit.",
    )
    # --- incident injection (scenario / decision-support twin, Increment 1) ---
    p.add_argument(
        "--incident-file",
        default=None,
        metavar="PATH",
        help="JSON incident spec to inject during the run (close_edge/close_lane/"
        "set_speed/add_vehicles). See incidents.py.",
    )
    p.add_argument(
        "--close-edge",
        action="append",
        default=[],
        metavar="EDGE@START[:END]",
        help="Quick edge closure, e.g. E123@23460:23760. Repeatable. Shorthand for a "
        "close_edge incident without writing a spec file.",
    )
    return p


def _build_incidents(args) -> list:
    """Assemble the incident list from --incident-file and any --close-edge flags."""
    import json

    from incidents import parse_cli_incident, parse_incidents

    incidents = []
    if args.incident_file:
        with open(args.incident_file, encoding="utf-8") as f:
            incidents.extend(parse_incidents(json.load(f)))
    incidents.extend(parse_cli_incident(s) for s in (args.close_edge or []))
    return incidents


def main() -> int:
    args = build_parser().parse_args()
    if not _ensure_sumo_tools():
        print(
            "Could not locate SUMO. Install it and set SUMO_HOME "
            "(macOS/Linux) or install to C:\\Sumo (Windows).",
            file=sys.stderr,
        )
        return 1

    traci = _import_traci()

    if args.connect:
        print(f"Connecting to TraCI on port {args.port} ...")
        for attempt in range(30):
            try:
                traci.init(args.port)
                break
            except traci.exceptions.FatalTraCIError:
                if attempt == 29:
                    raise
                time.sleep(1.0)
        print("Connected.")
    else:
        binary = sumo_bin("sumo-gui" if args.gui else "sumo")
        cmd = [
            binary,
            "-c",
            str(args.sumocfg) if args.sumocfg else str(SUMOCFG),
            "-b",
            str(args.begin),
            "--step-length",
            str(args.step_length),
            "--start",
            "--delay",
            str(args.delay),
            "--no-step-log",
            "--duration-log.disable",
        ]
        print(f"Starting SUMO on TraCI port {args.port}:", " ".join(cmd))
        traci.start(cmd, port=args.port)

    controller = None
    incidents = _build_incidents(args)
    if incidents:
        controller = IncidentController(incidents)
        print(f"Incidents scheduled: {controller.summary}")

    try:
        if args.jump_to is not None and args.jump_to > traci.simulation.getTime():
            print(f"Jumping to {args.jump_to:.0f}s ({_fmt_clock(args.jump_to)}) ...")
            step_to(traci, args.jump_to, chunk=args.chunk)
            print("Jump complete.")

        if args.emit or args.emit_target:
            # Emitting path: run the loop inside an asyncio event loop so the
            # WebSocket server and/or cloud forwarder run concurrently with the
            # TraCI stepping.
            asyncio.run(_run_emitting(traci, args, controller))
        else:
            print("Running simulation ...")
            while True:
                t = traci.simulation.getTime()
                if args.end is not None and t >= args.end:
                    break
                if traci.simulation.getMinExpectedNumber() <= 0 and t >= (args.jump_to or 0):
                    break
                if controller is not None:
                    controller.step(traci, t)
                traci.simulationStep()
                if int(t) % 60 == 0:
                    _print_status(traci)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        traci.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
