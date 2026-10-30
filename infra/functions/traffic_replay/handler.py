"""
traffic-replay — HTTP (Lambda Function URL) handler for historical metric reads.

This is the read path: the dashboard's replay / history features call this instead
of querying InfluxDB directly (which would expose the token in the browser). Given
a time range and a field, it runs a Flux query and returns JSON.

    GET ...?start=-1h&stop=now&field=avgSpeed&every=1m
      start  relative (-1h, -30m) or RFC3339; default -1h
      stop   relative, "now", or RFC3339; default now
      field  one of the allowed metric fields; default avgSpeed
      every  optional aggregate window (e.g. 1m) — mean per window

Returns: {"field": "...", "points": [{"time": "...", "value": 1.23}, ...]}

Stdlib only (urllib). Token read from Secrets Manager, cached per container.
"""
import json
import os
import re
import urllib.error
import urllib.request

import boto3

INFLUXDB_URL = os.environ["INFLUXDB_URL"].rstrip("/")
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]
INFLUXDB_SECRET_ARN = os.environ["INFLUXDB_SECRET_ARN"]

# Allowlist so user input never reaches Flux unchecked.
ALLOWED_FIELDS = {"avgSpeed", "congestionIndex", "vehicleCount", "stoppedCount", "movingCount", "simTime"}
_RANGE_RE = re.compile(r"^(-?\d+[smhdwy]|now\(\)|now|\d{4}-\d{2}-\d{2}T[\d:.]+Z)$")
_EVERY_RE = re.compile(r"^\d+[smhdw]$")
_SCENARIO_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # guards the Flux scenario_id filter

_token = None


def _get_token():
    global _token
    if _token is None:
        sm = boto3.client("secretsmanager")
        _token = sm.get_secret_value(SecretId=INFLUXDB_SECRET_ARN)["SecretString"]
    return _token


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "access-control-allow-origin": "*"},
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    start = params.get("start", "-1h")
    stop = params.get("stop", "now")
    field = params.get("field", "avgSpeed")
    every = params.get("every")
    scenario_id = params.get("scenario")

    if field not in ALLOWED_FIELDS:
        return _resp(400, {"error": f"field must be one of {sorted(ALLOWED_FIELDS)}"})
    stop = "now()" if stop in ("now", "now()") else stop
    for label, val in (("start", start), ("stop", stop)):
        if not _RANGE_RE.match(val):
            return _resp(400, {"error": f"invalid {label}"})
    if every is not None and not _EVERY_RE.match(every):
        return _resp(400, {"error": "invalid every"})
    if scenario_id is not None and not _SCENARIO_RE.match(scenario_id):
        return _resp(400, {"error": "invalid scenario"})

    flux = (
        f'from(bucket: "{INFLUXDB_BUCKET}")\n'
        f"  |> range(start: {start}, stop: {stop})\n"
        f'  |> filter(fn: (r) => r._measurement == "traffic_metrics")\n'
        f'  |> filter(fn: (r) => r._field == "{field}")\n'
    )
    if scenario_id:
        # scenario_id validated by _SCENARIO_RE above — safe to interpolate.
        flux += f'  |> filter(fn: (r) => r.scenario_id == "{scenario_id}")\n'
    if every:
        flux += f"  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)\n"

    try:
        csv_text = _query(flux)
    except urllib.error.HTTPError as exc:
        return _resp(exc.code, {"error": exc.read().decode("utf-8", "replace")})

    return _resp(200, {"field": field, "scenario": scenario_id, "points": _parse_csv(csv_text)})


def _query(flux):
    url = f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}"
    req = urllib.request.Request(
        url,
        data=flux.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {_get_token()}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")


def _parse_csv(csv_text):
    """Pull (_time, _value) out of InfluxDB's annotated CSV into a compact list."""
    points = []
    header = None
    for row in csv_text.splitlines():
        if not row or row.startswith("#"):
            continue
        cols = row.split(",")
        if header is None:
            header = cols
            continue
        record = dict(zip(header, cols))
        t, v = record.get("_time"), record.get("_value")
        if t and v not in (None, ""):
            try:
                points.append({"time": t, "value": float(v)})
            except ValueError:
                pass
    return points
