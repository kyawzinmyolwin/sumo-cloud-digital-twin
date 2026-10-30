"""
Metrics writer: consume the emitter's WebSocket feed, aggregate each tick with
metrics.compute_tick_metrics(), and write the result into InfluxDB.

    run_traci.py --emit  ──ws://:8765──►  metrics_writer  ──►  InfluxDB
                                              │
                                     compute_tick_metrics()   (pure, tested)

Config (CLI flag > environment > .env file > default):
    --influx-url / INFLUXDB_URL     default http://localhost:8086
    --org        / INFLUXDB_ORG
    --bucket     / INFLUXDB_BUCKET  default traffic-metrics
    --token      / INFLUXDB_TOKEN

Run from 2D_simulation/ with the local InfluxDB up (docker compose up -d):
    python scripts/metrics_writer.py --sample-every 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from metrics import compute_tick_metrics

DEFAULT_WS_URL = "ws://localhost:8765"
DEFAULT_INFLUX_URL = "http://localhost:8086"
DEFAULT_BUCKET = "traffic-metrics"
DEFAULT_MEASUREMENT = "traffic_metrics"


def _load_dotenv() -> None:
    """Populate os.environ from the nearest .env (walking up from cwd).

    Only sets keys that are not already in the environment, so real env vars and
    CLI flags still win. Minimal parser: KEY=VALUE lines, ignores comments/blanks.
    """
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Write per-tick traffic metrics to InfluxDB.")
    p.add_argument("--ws-url", default=DEFAULT_WS_URL, help=f"Emitter WebSocket URL (default: {DEFAULT_WS_URL})")
    p.add_argument("--influx-url", default=None, help="InfluxDB URL (default: $INFLUXDB_URL or http://localhost:8086)")
    p.add_argument("--org", default=None, help="InfluxDB org (default: $INFLUXDB_ORG)")
    p.add_argument("--bucket", default=None, help="InfluxDB bucket (default: $INFLUXDB_BUCKET or traffic-metrics)")
    p.add_argument("--token", default=None, help="InfluxDB token (default: $INFLUXDB_TOKEN)")
    p.add_argument("--measurement", default=DEFAULT_MEASUREMENT, help=f"Measurement name (default: {DEFAULT_MEASUREMENT})")
    p.add_argument("--sample-every", type=int, default=1, metavar="N", help="Write every Nth tick (default: 1 = every tick)")
    p.add_argument("--max-points", type=int, default=0, metavar="N", help="Stop after writing N points (0 = unlimited; useful for testing)")
    p.add_argument("--connect-timeout", type=float, default=60.0, help="Seconds to keep retrying the WebSocket connection")
    return p


def _resolve(args) -> dict:
    """CLI flag > env var > default."""
    return {
        "influx_url": args.influx_url or os.environ.get("INFLUXDB_URL") or DEFAULT_INFLUX_URL,
        "org": args.org or os.environ.get("INFLUXDB_ORG"),
        "bucket": args.bucket or os.environ.get("INFLUXDB_BUCKET") or DEFAULT_BUCKET,
        "token": args.token or os.environ.get("INFLUXDB_TOKEN"),
    }


def _point(measurement: str, m: dict):
    """Build an InfluxDB Point from a metrics dict."""
    from influxdb_client import Point

    p = (
        Point(measurement)
        .tag("simId", str(m.get("simId", "unknown")))
        .tag("scenario_id", str(m.get("scenarioId", m.get("simId", "unknown"))))
        .field("vehicleCount", int(m["vehicleCount"]))
        .field("avgSpeed", float(m["avgSpeed"]))
        .field("congestionIndex", float(m["congestionIndex"]))
        .field("stoppedCount", int(m["stoppedCount"]))
        .field("movingCount", int(m["movingCount"]))
    )
    if m.get("simTime") is not None:
        p = p.field("simTime", float(m["simTime"]))
    return p


async def run(args) -> int:
    import websockets
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    cfg = _resolve(args)
    if not cfg["token"] or not cfg["org"]:
        print(
            "Missing InfluxDB token/org. Set INFLUXDB_TOKEN and INFLUXDB_ORG "
            "(e.g. via .env or --token/--org).",
            file=sys.stderr,
        )
        return 2

    client = InfluxDBClient(url=cfg["influx_url"], token=cfg["token"], org=cfg["org"])
    # Synchronous writes: each write() blocks until the point is stored, so the
    # counter matches what actually lands and there is no flush-on-close race.
    # Throughput is kept sane with --sample-every rather than client-side batching.
    write_api = client.write_api(write_options=SYNCHRONOUS)
    print(f"InfluxDB: {cfg['influx_url']} org={cfg['org']} bucket={cfg['bucket']}")

    # Retry the WebSocket connection until the emitter is up.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + args.connect_timeout
    ws = None
    while loop.time() < deadline:
        try:
            ws = await websockets.connect(args.ws_url)
            break
        except OSError:
            await asyncio.sleep(1)
    if ws is None:
        print(f"Could not connect to emitter at {args.ws_url}", file=sys.stderr)
        client.close()
        return 3
    print(f"Connected to emitter {args.ws_url}")

    tick = 0
    written = 0
    try:
        async for message in ws:
            tick += 1
            if tick % args.sample_every != 0:
                continue
            try:
                snapshot = json.loads(message)
            except json.JSONDecodeError:
                continue
            metrics = compute_tick_metrics(snapshot)
            write_api.write(bucket=cfg["bucket"], org=cfg["org"], record=_point(args.measurement, metrics))
            written += 1
            if written % 25 == 0 or written == 1:
                print(
                    f"  wrote #{written}: simTime={metrics['simTime']} "
                    f"count={metrics['vehicleCount']} avgSpeed={metrics['avgSpeed']} "
                    f"congestion={metrics['congestionIndex']}"
                )
            if args.max_points and written >= args.max_points:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping.")
    finally:
        await ws.close()
        client.close()  # flushes any batched points
    print(f"Done. Wrote {written} points to '{cfg['bucket']}'.")
    return 0


def main() -> int:
    _load_dotenv()
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
