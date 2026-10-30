"""
Cloud scenario-control Lambda — the cloud twin of ``control_server.py``.

Behind an authenticated HTTP API, it starts/stops named-preset simulation runs on
the EC2 sim host by rewriting the ``sumo-emitter`` service's scenario args (via SSM
Run Command) and restarting the service.

SAFETY: it only ever composes the command from a fixed preset allowlist (plus a
regex-validated edge id for a road block) — never from arbitrary input — so the
SSM/shell command cannot be turned into arbitrary execution. The endpoint is also
authenticated (a shared-token Lambda authorizer), because starting a run triggers
compute/cost on the account.

Routes (mirror control_server.py so the same dashboard panel drives either one):
    GET  /scenarios
    GET  /status
    POST /start?scenario=<name>
    POST /start_roadblock?edge=<id>&duration=<sec>
    POST /stop

The pure helpers (scenario_list / args_for_scenario / roadblock_args /
scenario_args_string) are unit-tested; only the AWS glue needs a live account.
"""
from __future__ import annotations

import json
import os
import re
import time

import boto3

_EDGE_RE = re.compile(r"^-?[A-Za-z0-9_.#:]{1,80}$")
ROADBLOCK_AT = 23400
SERVICE = "sumo-emitter"
ENV_FILE = "/etc/sumo-emitter.env"

# Scenario-specific run_traci args ONLY. The sim host's service ExecStart already
# adds --no-gui --emit-interval <n> --emit-target <ws>, so these must not repeat
# the emit flags. (The incident-file path is relative to the service
# WorkingDirectory = 2D_simulation, hence scripts/scenarios/...)
_COMMON = ["--real-time", "--speed", "5", "--jump-to", "23400", "--end", "27000"]
PRESETS = {
    "baseline_am": {
        "label": "Baseline — morning peak",
        "description": "Calibrated 06:30 morning traffic, no incident.",
        "args": _COMMON + ["--scenario-id", "baseline_am"],
    },
    "crash_arterial": {
        "label": "Crash — arterial closed",
        "description": "Same morning, a major arterial blocked 06:35–06:45.",
        "args": _COMMON + ["--scenario-id", "crash_arterial", "--close-edge=770109405#0@23700:24300"],
    },
    "roadworks": {
        "label": "Roadworks — reduced speed",
        "description": "Same morning, an incident file applied for the run.",
        "args": _COMMON + ["--scenario-id", "roadworks",
                           "--incident-file", "scripts/scenarios/incident_example.json"],
    },
}


def scenario_list():
    """Public view of the presets (id + label + description) for the dashboard."""
    return [{"id": k, "label": v["label"], "description": v["description"]}
            for k, v in PRESETS.items()]


def args_for_scenario(name):
    """The scenario-specific args for a preset. Raises KeyError for unknown ones."""
    if name not in PRESETS:
        raise KeyError(name)
    return list(PRESETS[name]["args"])


def roadblock_args(edge, duration=None):
    """Args that close a user-chosen ``edge`` from the run start. Validates the edge."""
    if not _EDGE_RE.match(edge or ""):
        raise ValueError("invalid edge id %r" % (edge,))
    window = "%s@%d" % (edge, ROADBLOCK_AT) + ((":%d" % (ROADBLOCK_AT + int(duration))) if duration else "")
    return list(_COMMON) + ["--scenario-id", "roadblock", "--close-edge=%s" % window]


def scenario_args_string(args):
    """Join validated args into the SCENARIO_ARGS value. Every token is from the
    preset allowlist or a regex-validated edge id, so the result is shell-safe."""
    return " ".join(args)


# --- AWS glue (clients created lazily so the pure helpers import without creds) ---
_clients = {}


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _sim_host_running():
    tag = os.environ["SIM_HOST_TAG"]
    r = _client("ec2").describe_instances(
        Filters=[{"Name": "tag:Name", "Values": [tag]},
                 {"Name": "instance-state-name", "Values": ["running"]}])
    return any(res.get("Instances") for res in r.get("Reservations", []))


def _send(commands):
    tag = os.environ["SIM_HOST_TAG"]
    return _client("ssm").send_command(
        Targets=[{"Key": "tag:Name", "Values": [tag]}],
        DocumentName="AWS-RunShellScript",
        Comment="sim-control",
        Parameters={"commands": commands})


def _set_state(scenario):
    _client("ssm").put_parameter(
        Name=os.environ["STATE_PARAM"], Type="String", Overwrite=True,
        Value=json.dumps({"scenario": scenario, "startedAt": time.time(), "running": True}))


def _clear_state():
    _client("ssm").put_parameter(
        Name=os.environ["STATE_PARAM"], Type="String", Overwrite=True,
        Value=json.dumps({"running": False}))


def _get_state():
    try:
        p = _client("ssm").get_parameter(Name=os.environ["STATE_PARAM"])
        return json.loads(p["Parameter"]["Value"])
    except Exception:  # noqa: BLE001 - no state yet / not found
        return {"running": False}


def _restart_with(args, scenario_label):
    scen = scenario_args_string(args)
    _send([
        "set -e",
        "cat > %s <<'SUMOEOF'" % ENV_FILE,
        "SCENARIO_ARGS=%s" % scen,
        "SUMOEOF",
        "systemctl restart %s" % SERVICE,
    ])
    _set_state(scenario_label)


def _resp(status, body):
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(body)}


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    q = event.get("queryStringParameters") or {}
    try:
        if route == "GET /scenarios":
            return _resp(200, {"scenarios": scenario_list()})

        if route == "GET /status":
            st = _get_state()
            running = bool(st.get("running"))
            return _resp(200, {
                "running": running,
                "scenario": st.get("scenario") if running else None,
                "uptimeSec": round(time.time() - st["startedAt"], 1) if running and st.get("startedAt") else 0,
                "writer": running,
            })

        if route == "POST /start":
            scenario = (q.get("scenario") or "").strip()
            if scenario not in PRESETS:
                return _resp(400, {"ok": False, "error": "unknown scenario %r" % scenario})
            if not _sim_host_running():
                return _resp(409, {"ok": False, "error":
                                   "sim host not running — set sim_host_enabled=true, apply, and wait for boot"})
            _restart_with(args_for_scenario(scenario), scenario)
            return _resp(200, {"ok": True, "scenario": scenario})

        if route == "POST /start_roadblock":
            edge = (q.get("edge") or "").strip()
            raw = q.get("duration")
            try:
                duration = int(raw) if raw not in (None, "", "0") else None
            except (TypeError, ValueError):
                return _resp(400, {"ok": False, "error": "bad duration"})
            if not _EDGE_RE.match(edge):
                return _resp(400, {"ok": False, "error": "invalid edge id %r" % edge})
            if not _sim_host_running():
                return _resp(409, {"ok": False, "error": "sim host not running"})
            _restart_with(roadblock_args(edge, duration), "roadblock:%s" % edge)
            return _resp(200, {"ok": True, "scenario": "roadblock:%s" % edge, "edge": edge})

        if route == "POST /stop":
            _send(["systemctl stop %s" % SERVICE])
            _clear_state()
            return _resp(200, {"ok": True, "stopped": True})

        return _resp(404, {"error": "not found", "route": route})
    except Exception as exc:  # noqa: BLE001 - surface the error to the dashboard
        return _resp(500, {"ok": False, "error": str(exc)})
