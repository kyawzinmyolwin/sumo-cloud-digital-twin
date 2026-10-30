#!/usr/bin/env python3
"""
Run one or more named scenarios back-to-back and land their metrics in InfluxDB,
so the dashboard Compare / Impact views have both runs to read.

Why sequential (not parallel): a run binds TraCI (8813) and the emitter WebSocket
(8765), and one metrics_writer reads that single feed — two at once would collide.
Each preset also self-terminates (``--end 27000`` = 07:30), so the next one can
start cleanly once the previous finishes.

This reuses the SAME preset registry the dashboard's control server uses
(``control_server.PRESETS`` via ``build_command``), so the scenarios here never
drift from the Start buttons. For each scenario it:

    spawn run_traci.py (the preset)  ->  spawn metrics_writer.py against the feed
    ->  wait for the run to finish   ->  stop the writer  ->  next scenario

Examples:
    # the usual compare pair (default), real-time like the dashboard:
    python scripts/run_scenarios.py

    # just one scenario:
    python scripts/run_scenarios.py --scenario baseline_am

    # populate InfluxDB as fast as possible (drop real-time pacing):
    python scripts/run_scenarios.py --fast

    # don't auto-start the writer (you run it yourself):
    python scripts/run_scenarios.py --no-writer

InfluxDB config is taken by metrics_writer.py from the usual env
(``INFLUXDB_URL`` / ``INFLUXDB_ORG`` / ``INFLUXDB_BUCKET`` / ``INFLUXDB_TOKEN``;
defaults to local ``http://localhost:8086`` / ``traffic-metrics``).
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

# Reuse the dashboard's validated preset registry so the two never diverge.
from control_server import PRESETS, SCRIPTS_DIR, build_command

# The two scenarios the Compare view is designed around.
DEFAULT_SCENARIOS = ["baseline_am", "crash_arterial"]
# metrics_writer connects to the emitter here (presets emit on 0.0.0.0:8765).
DEFAULT_WS_PORT = 8765


def scenarios_to_run(requested: list[str] | None) -> list[str]:
    """The ordered scenario list to execute (default = the compare pair)."""
    return list(requested) if requested else list(DEFAULT_SCENARIOS)


def maybe_fast(cmd: list[str], fast: bool) -> list[str]:
    """Strip ``--real-time`` for a quick data-generation run (writes to InfluxDB
    as fast as the sim steps). ``--speed`` is inert without ``--real-time``, so it
    can stay. Leaves the command untouched when ``fast`` is False."""
    if not fast:
        return list(cmd)
    return [a for a in cmd if a != "--real-time"]


def _run_dir() -> str:
    """run_traci.py / metrics_writer.py expect the 2D_simulation dir as cwd (so the
    SUMO config and data paths resolve) — same cwd control_server uses."""
    return os.path.dirname(SCRIPTS_DIR)


def _spawn_writer(python: str, ws_port: int):
    cmd = [python, os.path.join(SCRIPTS_DIR, "metrics_writer.py"),
           "--ws-url", "ws://localhost:%d" % ws_port]
    try:
        return subprocess.Popen(cmd, cwd=_run_dir())
    except Exception as exc:  # noqa: BLE001 - a missing writer shouldn't abort the batch
        print("warning: could not start metrics_writer: %s" % exc, file=sys.stderr)
        return None


def _terminate(proc, name: str, timeout: float = 10.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print("  %s did not stop in %.0fs — killing" % (name, timeout))
        proc.kill()


def run_one(scenario: str, *, python: str, ws_port: int, fast: bool,
            start_writer: bool, writer_warmup: float = 2.0) -> int:
    """Run a single scenario to completion; return run_traci's exit code."""
    cmd = maybe_fast(build_command(scenario, python=python), fast)
    print("\n=== %s : %s ===" % (scenario, PRESETS[scenario]["label"]))
    print("  " + " ".join(cmd))
    sim = subprocess.Popen(cmd, cwd=_run_dir())

    writer = None
    if start_writer:
        # Give the emitter a moment to bind its WebSocket before the writer dials in.
        time.sleep(writer_warmup)
        if sim.poll() is None:
            writer = _spawn_writer(python, ws_port)
            print("  metrics_writer -> ws://localhost:%d" % ws_port)

    try:
        sim.wait()
    finally:
        _terminate(writer, "metrics_writer")
    print("  %s finished (exit %s)" % (scenario, sim.returncode))
    return sim.returncode or 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run scenarios back-to-back into InfluxDB for the Compare view.")
    p.add_argument("--scenario", action="append", choices=sorted(PRESETS),
                   help="Scenario to run (repeatable). Default: %s." % " + ".join(DEFAULT_SCENARIOS))
    p.add_argument("--fast", action="store_true",
                   help="Drop --real-time pacing (populate InfluxDB as fast as the sim runs).")
    p.add_argument("--no-writer", action="store_true",
                   help="Don't auto-start metrics_writer (run it yourself if you want history).")
    p.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT,
                   help="Emitter WebSocket port the writer connects to (default: %d)." % DEFAULT_WS_PORT)
    p.add_argument("--gap", type=float, default=5.0,
                   help="Seconds to wait between scenarios (default: 5).")
    p.add_argument("--python", default=sys.executable, help="Python interpreter for the child processes.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    order = scenarios_to_run(args.scenario)
    print("Scenarios (in order): %s" % ", ".join(order))

    rc = 0
    for i, scenario in enumerate(order):
        try:
            code = run_one(scenario, python=args.python, ws_port=args.ws_port,
                           fast=args.fast, start_writer=not args.no_writer)
        except KeyboardInterrupt:
            print("\ninterrupted — stopping.")
            return 130
        rc = rc or code
        if i < len(order) - 1 and args.gap > 0:
            print("  waiting %.0fs before the next scenario..." % args.gap)
            time.sleep(args.gap)

    print("\nAll done. Open the dashboard Compare view and set the two scenario ids: %s"
          % " vs ".join(order[:2]) if len(order) >= 2 else "\nDone.")
    return rc


if __name__ == "__main__":
    # Clean Ctrl-C: let KeyboardInterrupt propagate to main() for tidy child shutdown.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
