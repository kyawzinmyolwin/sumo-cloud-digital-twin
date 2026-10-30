"""
traffic-metrics — invoked (async) by traffic-ingest with one emitter snapshot.

Computes the per-tick metrics with the SAME pure function used locally
(compute_tick_metrics, copied from scripts/metrics.py — no changes) and writes one
line-protocol point to InfluxDB Cloud over the stdlib (urllib) so there is nothing
to vendor into the deployment package.

Env: INFLUXDB_URL, INFLUXDB_ORG, INFLUXDB_BUCKET, INFLUXDB_SECRET_ARN.
The token is read from Secrets Manager once per container and cached.
"""
import json
import os
import urllib.error
import urllib.request

import boto3

from metrics import compute_tick_metrics

INFLUXDB_URL = os.environ["INFLUXDB_URL"].rstrip("/")
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]
INFLUXDB_SECRET_ARN = os.environ["INFLUXDB_SECRET_ARN"]

_token = None  # cached across warm invocations


def _get_token():
    global _token
    if _token is None:
        sm = boto3.client("secretsmanager")
        _token = sm.get_secret_value(SecretId=INFLUXDB_SECRET_ARN)["SecretString"]
    return _token


def _line_protocol(m):
    # measurement,tagset fieldset   (timestamp omitted → InfluxDB uses server time)
    sim_id = str(m.get("simId", "unknown")).replace(" ", "_")
    scenario_id = str(m.get("scenarioId", m.get("simId", "unknown"))).replace(" ", "_")
    fields = [
        f"vehicleCount={int(m['vehicleCount'])}i",
        f"avgSpeed={float(m['avgSpeed'])}",
        f"congestionIndex={float(m['congestionIndex'])}",
        f"stoppedCount={int(m['stoppedCount'])}i",
        f"movingCount={int(m['movingCount'])}i",
    ]
    if m.get("simTime") is not None:
        fields.append(f"simTime={float(m['simTime'])}")
    return f"traffic_metrics,simId={sim_id},scenario_id={scenario_id} {','.join(fields)}"


def lambda_handler(event, context):
    # event is the snapshot dict passed by traffic-ingest.
    metrics = compute_tick_metrics(event)
    line = _line_protocol(metrics)

    url = f"{INFLUXDB_URL}/api/v2/write?org={INFLUXDB_ORG}&bucket={INFLUXDB_BUCKET}&precision=ns"
    req = urllib.request.Request(
        url,
        data=line.encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {_get_token()}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        # Log the exact line written. CloudWatch tags every log line with the
        # invocation RequestId, so this one line lets a CloudWatch record be
        # matched 1:1 to the resulting InfluxDB point (same scenario_id + field
        # values) without storing the RequestId in InfluxDB.
        print(f"wrote point: {line}")
    except urllib.error.HTTPError as exc:
        # Surface the InfluxDB error body in the logs for debugging.
        print(f"InfluxDB write failed {exc.code}: {exc.read().decode('utf-8', 'replace')}")
        raise
    return {"ok": True, "vehicleCount": metrics["vehicleCount"]}
