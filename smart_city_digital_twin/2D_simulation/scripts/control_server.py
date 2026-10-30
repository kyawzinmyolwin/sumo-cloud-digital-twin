#!/usr/bin/env python3
"""
Scenario control server — lets the dashboard start/stop the simulation from the
browser, so a non-IT user never touches the command line.

The browser can't launch a process, so this tiny HTTP server (stdlib only) runs on
the machine that has SUMO and spawns ``run_traci.py`` on request. For safety it only
starts **named presets** from a fixed registry — never arbitrary arguments — so a
process-spawning endpoint can't be turned into arbitrary command execution.

    GET  /scenarios              -> the presets a user can start
    GET  /status                 -> {running, scenario, pid, uptimeSec}
    POST /start?scenario=<name>  -> spawn run_traci.py for that preset (one at a time)
    POST /stop                   -> terminate the running sim

Run it on the VM next to the emitter:
    python scripts/control_server.py            # serves http://localhost:8799
Point the dashboard at it with ?control=http://localhost:8799 (default).

The preset registry (PRESETS) is plain data — edit the edge ids / windows to match
your net. Pure helpers (arg building, validation, status) are unit-tested without
spawning anything.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8799

# A SUMO edge id: letters/digits and _ . # : with an optional leading '-' (reverse
# direction). No '@' or spaces — the window suffix is added by us, not the user. This
# guards the value that goes into the spawned command (passed as --close-edge=<id> so a
# leading '-' can't be read as a flag; Popen uses an arg list, so there's no shell either).
_EDGE_RE = re.compile(r"^-?[A-Za-z0-9_.#:]{1,80}$")
# Close from the run's start (06:30 = the --jump-to in _COMMON) so a click blocks the road
# immediately — a non-IT user shouldn't have to know a sim-time or wait for a hidden trigger.
# Keep this equal to the --jump-to value in _COMMON.
ROADBLOCK_AT = 23400

# Preset scenarios a browser user can start. Each maps to a *fixed* run_traci.py
# argument list (no user-supplied args reach the shell). Edit the edge id / window
# to match the deployed net; keep --emit --emit-host 0.0.0.0 so the dashboard sees it.
_COMMON = ["--no-gui", "--emit", "--emit-host", "0.0.0.0", "--real-time", "--speed", "5",
           "--jump-to", "23400", "--end", "27000"]
PRESETS: dict[str, dict] = {
    "baseline_am": {
        "label": "Baseline — morning peak",
        "description": "Calibrated 06:30 morning traffic, no incident.",
        "args": _COMMON + ["--scenario-id", "baseline_am"],
    },
    "crash_arterial": {
        "label": "Crash — arterial closed",
        "description": "Same morning, a major arterial blocked 06:35–06:45.",
        "args": _COMMON + ["--scenario-id", "crash_arterial",
                           "--close-edge", "770109405#0@23700:24300"],
    },
    "roadworks": {
        "label": "Roadworks — reduced speed",
        "description": "Same morning, a corridor limited to 4 m/s for the run.",
        "args": _COMMON + ["--scenario-id", "roadworks",
                           "--incident-file", "scenarios/incident_example.json"],
    },
}


def scenario_list() -> list[dict]:
    """Public view of the presets (id + label + description) for the dashboard."""
    return [{"id": k, "label": v["label"], "description": v["description"]}
            for k, v in PRESETS.items()]


def build_command(scenario: str, *, python: str | None = None, script_dir: str = SCRIPTS_DIR) -> list[str]:
    """The exact run_traci.py command for a preset. Raises KeyError for unknown ones."""
    if scenario not in PRESETS:
        raise KeyError(scenario)
    py = python or sys.executable
    return [py, os.path.join(script_dir, "run_traci.py")] + list(PRESETS[scenario]["args"])


def build_roadblock_command(edge: str, *, at: int = ROADBLOCK_AT, duration: int | None = None,
                            python: str | None = None, script_dir: str = SCRIPTS_DIR) -> list[str]:
    """run_traci.py command that closes a user-chosen ``edge`` from a fresh run.

    ``edge`` must match _EDGE_RE (validated here too, not just at the HTTP layer).
    Passed as ``--close-edge=<edge>@<at>[:<at+duration>]`` so a leading '-' edge id
    isn't parsed as a flag.
    """
    if not _EDGE_RE.match(edge or ""):
        raise ValueError(f"invalid edge id {edge!r}")
    window = f"{edge}@{at}" + (f":{at + int(duration)}" if duration else "")
    py = python or sys.executable
    return ([py, os.path.join(script_dir, "run_traci.py")] + list(_COMMON)
            + ["--scenario-id", "roadblock", f"--close-edge={window}"])


class SimRunner:
    """Owns at most one run_traci.py subprocess (start / stop / status)."""

    def __init__(self, *, popen=subprocess.Popen, script_dir: str = SCRIPTS_DIR,
                 start_writer: bool = True, emit_port: int = 8765) -> None:
        self._popen = popen
        self._script_dir = script_dir
        self._start_writer = start_writer
        self._emit_port = emit_port
        self._proc = None
        self._writer = None
        self._scenario: str | None = None
        self._started_at: float = 0.0

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn_writer(self) -> None:
        """Also start metrics_writer so the run's metrics land in InfluxDB — the GUI
        path otherwise leaves History/Compare empty (the 'forgot the writer' trap)."""
        if not self._start_writer:
            return
        cmd = [sys.executable, os.path.join(self._script_dir, "metrics_writer.py"),
               "--ws-url", f"ws://localhost:{self._emit_port}"]
        try:
            self._writer = self._popen(cmd, cwd=os.path.dirname(self._script_dir))
        except Exception as exc:  # noqa: BLE001 - a missing writer shouldn't fail the sim start
            print(f"warning: could not start metrics_writer: {exc}")
            self._writer = None

    def _stop_writer(self) -> None:
        if self._writer is None:
            return
        try:
            self._writer.terminate()
            self._writer.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                self._writer.kill()
            except Exception:  # noqa: BLE001
                pass
        self._writer = None

    def start(self, scenario: str) -> dict:
        if scenario not in PRESETS:
            return {"ok": False, "error": f"unknown scenario {scenario!r}"}
        if self.running():
            return {"ok": False, "error": f"already running {self._scenario!r}; stop it first"}
        cmd = build_command(scenario, script_dir=self._script_dir)
        # cwd = 2D_simulation so relative paths (scenarios/…, data/…) resolve like the CLI.
        self._proc = self._popen(cmd, cwd=os.path.dirname(self._script_dir))
        self._scenario = scenario
        self._started_at = time.time()
        self._spawn_writer()
        return {"ok": True, "scenario": scenario, "pid": getattr(self._proc, "pid", None)}

    def start_roadblock(self, edge: str, *, duration: int | None = None) -> dict:
        """Start a fresh run with ``edge`` closed (the map 'Start road block' button)."""
        if not _EDGE_RE.match(edge or ""):
            return {"ok": False, "error": f"invalid edge id {edge!r}"}
        if self.running():
            return {"ok": False, "error": f"already running {self._scenario!r}; stop it first"}
        cmd = build_roadblock_command(edge, duration=duration, script_dir=self._script_dir)
        self._proc = self._popen(cmd, cwd=os.path.dirname(self._script_dir))
        self._scenario = f"roadblock:{edge}"
        self._started_at = time.time()
        self._spawn_writer()
        return {"ok": True, "scenario": self._scenario, "edge": edge,
                "pid": getattr(self._proc, "pid", None)}

    def stop(self) -> dict:
        self._stop_writer()
        if not self.running():
            return {"ok": True, "stopped": False}  # nothing to do
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self._proc.kill()
        stopped = self._scenario
        self._scenario = None
        return {"ok": True, "stopped": True, "scenario": stopped}

    def status(self) -> dict:
        run = self.running()
        return {
            "running": run,
            "scenario": self._scenario if run else None,
            "pid": getattr(self._proc, "pid", None) if run else None,
            "uptimeSec": round(time.time() - self._started_at, 1) if run else 0,
            "writer": self._writer is not None and self._writer.poll() is None,
        }


def make_handler(runner: SimRunner):
    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quieter logs
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(204, {})

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path in ("", "/scenarios"):
                return self._send(200, {"scenarios": scenario_list()})
            if path == "/status":
                return self._send(200, runner.status())
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            if path == "/start":
                scenario = (parse_qs(parsed.query).get("scenario", [""])[0]).strip()
                result = runner.start(scenario)
                return self._send(200 if result.get("ok") else 400, result)
            if path == "/start_roadblock":
                q = parse_qs(parsed.query)
                edge = (q.get("edge", [""])[0]).strip()
                raw = q.get("duration", [None])[0]
                try:
                    duration = int(raw) if raw not in (None, "", "0") else None
                except ValueError:
                    return self._send(400, {"ok": False, "error": "bad duration"})
                result = runner.start_roadblock(edge, duration=duration)
                return self._send(200 if result.get("ok") else 400, result)
            if path == "/stop":
                return self._send(200, runner.stop())
            return self._send(404, {"error": "not found"})

    return ControlHandler


def main() -> int:
    ap = argparse.ArgumentParser(description="Start/stop sim scenarios for the dashboard.")
    ap.add_argument("--host", default="0.0.0.0", help="Bind host (default 0.0.0.0).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default {DEFAULT_PORT}).")
    ap.add_argument("--no-writer", action="store_true",
                    help="Don't auto-start metrics_writer with each scenario (then run it yourself "
                         "if you want History/Compare data).")
    args = ap.parse_args()

    runner = SimRunner(start_writer=not args.no_writer)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(runner))
    print(f"Scenario control server on http://{args.host}:{args.port}  "
          f"(presets: {', '.join(PRESETS)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
