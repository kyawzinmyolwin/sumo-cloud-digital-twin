"""
Capture baseline-vs-incident scenario results for the final report.

Queries the replay endpoint (the local replay_server, or the cloud replay API) for
each scenario's stored metrics, aggregates them (mean + peak), and prints a Markdown
table of baseline-vs-incident impact — the same numbers the dashboard Impact report
shows, but on the command line so they paste straight into the report (§8, Table 1).

Prereqs: the stack running and each scenario already run (so InfluxDB has data).
    docker compose up         # or ./start_demo.sh, then start each preset
    python scripts/capture_results.py \
        --replay http://localhost:8788/metrics \
        --scenarios baseline_am,crash_arterial,roadworks

The aggregation/delta helpers are pure and unit-tested (tests/test_capture_results.py);
only fetch() touches the network.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Fields pulled per scenario. avgSpeed is in m/s and reported in km/h.
FIELDS = ["avgSpeed", "congestionIndex", "stoppedCount", "vehicleCount"]
MS_TO_KMH = 3.6


def summarise(points: list[dict]) -> dict:
    """Mean, peak (max) and sample count for a list of {time, value} points."""
    values = [p["value"] for p in points if isinstance(p.get("value"), (int, float))]
    if not values:
        return {"mean": None, "max": None, "n": 0}
    return {"mean": sum(values) / len(values), "max": max(values), "n": len(values)}


def pct_drop(base: float | None, scn: float | None) -> float | None:
    """Percent drop from baseline to scenario (positive = scenario is lower/slower)."""
    if base is None or scn is None or base == 0:
        return None
    return (base - scn) / base * 100.0


def delta(base: float | None, scn: float | None) -> float | None:
    """Signed change scenario - baseline (positive = scenario is higher)."""
    if base is None or scn is None:
        return None
    return scn - base


def fetch(replay_url: str, field: str, scenario: str, start: str, stop: str) -> list[dict]:
    """GET raw (non-aggregated) points for one field + scenario from the replay API."""
    q = urllib.parse.urlencode({"field": field, "scenario": scenario, "start": start, "stop": stop})
    req = urllib.request.Request(f"{replay_url}?{q}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("points", [])


def collect(replay_url: str, scenario: str, start: str, stop: str) -> dict:
    """All field summaries for one scenario, keyed by field name."""
    out = {}
    for field in FIELDS:
        try:
            out[field] = summarise(fetch(replay_url, field, scenario, start, stop))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            print(f"  warning: {scenario}/{field}: {exc}", file=sys.stderr)
            out[field] = {"mean": None, "max": None, "n": 0}
    return out


def _fmt(v: float | None, digits: int = 1, suffix: str = "") -> str:
    return "—" if v is None else f"{v:.{digits}f}{suffix}"


def build_table(scenarios: list[str], data: dict[str, dict]) -> str:
    """Render Table 1 (Markdown). First scenario in the list is the baseline."""
    base = data[scenarios[0]]
    base_speed = base["avgSpeed"]["mean"]
    base_cong = base["congestionIndex"]["mean"]
    rows = [
        "| Scenario | Avg speed (km/h) | Δ speed | Congestion index | Δ congestion | Stopped (mean / peak) | Vehicles (mean) |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, scn in enumerate(scenarios):
        d = data[scn]
        speed_kmh = None if d["avgSpeed"]["mean"] is None else d["avgSpeed"]["mean"] * MS_TO_KMH
        base_speed_kmh = None if base_speed is None else base_speed * MS_TO_KMH
        if i == 0:
            dspeed, dcong = "—", "—"
        else:
            drop = pct_drop(base_speed_kmh, speed_kmh)
            rise = delta(base_cong, d["congestionIndex"]["mean"])
            dspeed = "—" if drop is None else f"{-drop:+.1f}%"   # negative = slower
            dcong = "—" if rise is None else f"{rise:+.2f}"
        stopped = f"{_fmt(d['stoppedCount']['mean'])} / {_fmt(d['stoppedCount']['max'], 0)}"
        rows.append(
            f"| `{scn}` | {_fmt(speed_kmh)} | {dspeed} | {_fmt(d['congestionIndex']['mean'], 2)} "
            f"| {dcong} | {stopped} | {_fmt(d['vehicleCount']['mean'], 0)} |"
        )
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture baseline-vs-incident scenario results.")
    ap.add_argument("--replay", default="http://localhost:8788/metrics",
                    help="Replay endpoint (default local replay_server).")
    ap.add_argument("--scenarios", default="baseline_am,crash_arterial,roadworks",
                    help="Comma-separated scenario_ids; the FIRST is the baseline.")
    ap.add_argument("--start", default="-6h", help="Range start (default -6h).")
    ap.add_argument("--stop", default="now", help="Range stop (default now).")
    args = ap.parse_args()

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    if len(scenarios) < 2:
        print("Need at least a baseline and one incident scenario.", file=sys.stderr)
        return 2

    data = {scn: collect(args.replay, scn, args.start, args.stop) for scn in scenarios}
    empty = [s for s in scenarios if all(data[s][f]["n"] == 0 for f in FIELDS)]
    if empty:
        print(f"warning: no data for: {', '.join(empty)} "
              f"(did the scenario run and write to InfluxDB?)", file=sys.stderr)

    print("\n**Table 1. Baseline vs incident impact (morning peak).**\n")
    print(build_table(scenarios, data))
    print("\n_Δ speed negative = slower than baseline; Δ congestion positive = more congested._")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
