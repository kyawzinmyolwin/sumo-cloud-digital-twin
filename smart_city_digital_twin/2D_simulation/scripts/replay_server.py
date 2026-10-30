"""
Local replay read endpoint — the dev-time mirror of the traffic-replay Lambda.

The dashboard's historical charts need a read endpoint, but the browser must not
hold the InfluxDB token, and the cloud Function URL is blocked on this account.
So this tiny server holds the token and proxies range queries to InfluxDB, with
CORS so the map (served on :8000) can fetch from it. Same JSON contract as the
Lambda, so the UI can later point at an API-Gateway-fronted cloud endpoint by
changing one URL.

    GET /metrics?start=-1h&stop=now&field=avgSpeed&every=1m
      -> {"field": "avgSpeed", "points": [{"time": "...", "value": 6.6}, ...]}

Run from 2D_simulation/ with InfluxDB reachable (local Docker, or point at cloud):
    python scripts/replay_server.py                 # :8788, queries local InfluxDB
Config (CLI > env > .env > default):
    --influx-url / INFLUXDB_URL     default http://localhost:8086
    --org        / INFLUXDB_ORG
    --bucket     / INFLUXDB_BUCKET  default traffic-metrics
    --token      / INFLUXDB_TOKEN
    --port                          default 8788
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_INFLUX_URL = "http://localhost:8086"
DEFAULT_BUCKET = "traffic-metrics"
DEFAULT_PORT = 8788

# Same allowlist + validation as the Lambda — user input never reaches Flux raw.
ALLOWED_FIELDS = {"avgSpeed", "congestionIndex", "vehicleCount", "stoppedCount", "movingCount", "simTime"}
_RANGE_RE = re.compile(r"^(-?\d+[smhdwy]|now\(\)|now|\d{4}-\d{2}-\d{2}T[\d:.]+Z)$")
_EVERY_RE = re.compile(r"^\d+[smhdw]$")
_SCENARIO_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # guards the Flux scenario_id filter


def _load_dotenv() -> None:
    """Populate os.environ from the nearest .env (walk up from cwd). Env wins."""
    here = Path.cwd()
    for directory in (here, *here.parents):
        env_file = directory / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
            return


def build_flux(bucket: str, start: str, stop: str, field: str, every: str | None,
               scenario_id: str | None = None) -> str:
    flux = (
        f'from(bucket: "{bucket}")\n'
        f"  |> range(start: {start}, stop: {stop})\n"
        f'  |> filter(fn: (r) => r._measurement == "traffic_metrics")\n'
        f'  |> filter(fn: (r) => r._field == "{field}")\n'
    )
    if scenario_id:
        # scenario_id is validated by the caller (_SCENARIO_RE) — safe to interpolate.
        flux += f'  |> filter(fn: (r) => r.scenario_id == "{scenario_id}")\n'
    if every:
        flux += f"  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)\n"
    return flux


def query_metrics(cfg: dict, start: str, stop: str, field: str, every: str | None,
                  scenario_id: str | None = None) -> list[dict]:
    flux = build_flux(cfg["bucket"], start, stop, field, every, scenario_id)
    url = f"{cfg['influx_url']}/api/v2/query?org={cfg['org']}"
    req = urllib.request.Request(
        url,
        data=flux.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {cfg['token']}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return _parse_csv(resp.read().decode("utf-8"))


def _parse_csv(csv_text: str) -> list[dict]:
    points: list[dict] = []
    header = None
    for row in csv_text.splitlines():
        if not row or row.startswith("#"):
            continue
        cols = row.split(",")
        if header is None:
            header = cols
            continue
        rec = dict(zip(header, cols))
        t, v = rec.get("_time"), rec.get("_value")
        if t and v not in (None, ""):
            try:
                points.append({"time": t, "value": float(v)})
            except ValueError:
                pass
    return points


def make_handler(cfg: dict):
    class ReplayHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quieter logs
            pass

        def _send(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):  # CORS preflight
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") not in ("", "/metrics"):
                return self._send(404, {"error": "not found"})
            q = parse_qs(parsed.query)
            start = q.get("start", ["-1h"])[0]
            stop = q.get("stop", ["now"])[0]
            field = q.get("field", ["avgSpeed"])[0]
            every = q.get("every", [None])[0]
            scenario_id = q.get("scenario", [None])[0]

            if field not in ALLOWED_FIELDS:
                return self._send(400, {"error": f"field must be one of {sorted(ALLOWED_FIELDS)}"})
            stop = "now()" if stop in ("now", "now()") else stop
            for label, val in (("start", start), ("stop", stop)):
                if not _RANGE_RE.match(val):
                    return self._send(400, {"error": f"invalid {label}"})
            if every is not None and not _EVERY_RE.match(every):
                return self._send(400, {"error": "invalid every"})
            if scenario_id is not None and not _SCENARIO_RE.match(scenario_id):
                return self._send(400, {"error": "invalid scenario"})

            try:
                points = query_metrics(cfg, start, stop, field, every, scenario_id)
            except urllib.error.HTTPError as exc:
                return self._send(exc.code, {"error": exc.read().decode("utf-8", "replace")})
            except urllib.error.URLError as exc:
                return self._send(502, {"error": f"InfluxDB unreachable: {exc}"})
            return self._send(200, {"field": field, "scenario": scenario_id, "points": points})

    return ReplayHandler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local replay read endpoint for the dashboard.")
    p.add_argument("--influx-url", default=None)
    p.add_argument("--org", default=None)
    p.add_argument("--bucket", default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind host (default 0.0.0.0 so it's reachable through VM port-forwarding; "
                        "use 127.0.0.1 to restrict to this machine).")
    return p


def main() -> int:
    _load_dotenv()
    args = build_parser().parse_args()
    cfg = {
        "influx_url": (args.influx_url or os.environ.get("INFLUXDB_URL") or DEFAULT_INFLUX_URL).rstrip("/"),
        "org": args.org or os.environ.get("INFLUXDB_ORG"),
        "bucket": args.bucket or os.environ.get("INFLUXDB_BUCKET") or DEFAULT_BUCKET,
        "token": args.token or os.environ.get("INFLUXDB_TOKEN"),
    }
    if not cfg["token"] or not cfg["org"]:
        print("Missing INFLUXDB_TOKEN / INFLUXDB_ORG (set via .env or flags).", flush=True)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cfg))
    print(f"Replay server on http://{args.host}:{args.port}/metrics "
          f"→ {cfg['influx_url']} (bucket {cfg['bucket']})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
