"""
Validate the simulation baseline against the real traffic counts it was built from.

This is the *model-validation* companion to the unit tests: rather than checking the
code, it checks that the running simulation reproduces the real Christchurch City
Council / Miovision turning-movement counts that generated its demand.

How it works
------------
The demand loaded by SUMO (``data/output/demand/traffic_trips.rou.xml``) encodes the
real counts directly: every surveyed movement is a ``<flow>`` with a vehicle ``number``
inserted on a specific ``from`` edge in a 15-minute slot. Summing those numbers per
``from`` edge over a time window gives the **observed** demand placed on the network.

The **simulated** throughput on the same edges comes from a SUMO ``edgeData`` output
collected over the same window (see the run recipe below). We compare observed vs
simulated per edge with the **GEH statistic** — the standard traffic-engineering measure
for modelled-vs-observed volumes (GEH < 5 is an accepted match; the usual target is
GEH < 5 for >= 85% of links).

Honest framing: because the demand was *generated from* these counts, this validates that
the end-to-end pipeline (demand build -> routing -> microsimulation, including vehicle
insertion under congestion) faithfully reproduces the surveyed volumes at the surveyed
locations. It is a calibration/consistency check, not an independent field validation.

Run recipe (produce the simulated side on a machine with SUMO)
--------------------------------------------------------------
1. Write an edgeData additional file, e.g. ``edgedata.add.xml``::

       <additional>
         <edgeData id="val" file="edgedata_out.xml" begin="23400" end="27000"/>
       </additional>

2. Run the baseline headless over the same window (06:30-07:30 = 23400-27000)::

       sumo -c Christchurch_Central_City_main_streets.sumocfg \
            --additional-files data/output/network/Christchurch_Central_City_main_streets.add.xml,edgedata.add.xml \
            --end 27000

3. Compare::

       python scripts/validate_against_counts.py --sim edgedata_out.xml \
            --begin 23400 --end 27000

Without ``--sim`` you can pass ``--routed data/output/demand/traffic_trips.routed.rou.xml``
for a preliminary *demand-loading* check (how many vehicles routing placed on each edge);
this does not capture congestion effects, so the ``--sim`` edgeData run is the one to cite.

The scoring helpers (``geh``, ``compare``, ``summarise``) are pure and unit-tested
(tests/test_validate_against_counts.py); only the parsers touch files.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET

# Default AM-peak window matching the baseline_am preset: 06:30-07:30 (sim seconds).
DEFAULT_BEGIN = 23400.0
DEFAULT_END = 27000.0
# GEH field reported from edgeData. "departed" = vehicles inserted on the edge (the
# matched comparison to observed demand); "entered" = total vehicles that drove onto it.
DEFAULT_FIELD = "departed"


def geh(modelled: float, observed: float) -> float:
    """GEH statistic between a modelled and an observed volume.

    GEH = sqrt( 2 * (M - C)^2 / (M + C) ). Returns 0.0 when both are 0.
    """
    m = float(modelled)
    c = float(observed)
    if m + c == 0:
        return 0.0
    return (2.0 * (m - c) ** 2 / (m + c)) ** 0.5


def observed_by_edge(flows_path: str, begin: float, end: float) -> dict[str, int]:
    """Observed demand per ``from`` edge: sum of flow ``number`` whose ``begin`` is in
    [begin, end). Reads the count-derived flow file (traffic_trips.rou.xml)."""
    with open(flows_path, encoding="utf-8") as fh:
        text = fh.read()
    out: dict[str, int] = {}
    for tag in re.finditer(r"<flow\b[^>]*>", text):
        s = tag.group(0)
        b = re.search(r'\bbegin="([0-9.]+)"', s)
        frm = re.search(r'\bfrom="([^"]+)"', s)
        num = re.search(r'\bnumber="([0-9]+)"', s)
        if not (b and frm and num):
            continue
        if begin <= float(b.group(1)) < end:
            out[frm.group(1)] = out.get(frm.group(1), 0) + int(num.group(1))
    return out


def sim_by_edge(edgedata_path: str, begin: float, end: float, field: str) -> dict[str, float]:
    """Simulated per-edge counts from a SUMO edgeData output: sum ``field`` across all
    <edge> entries in <interval>s overlapping [begin, end)."""
    out: dict[str, float] = {}
    root = ET.parse(edgedata_path).getroot()
    for interval in root.iter("interval"):
        ib = float(interval.get("begin", "nan"))
        ie = float(interval.get("end", "nan"))
        # keep intervals that overlap the requested window
        if ie <= begin or ib >= end:
            continue
        for edge in interval.iter("edge"):
            eid = edge.get("id")
            val = edge.get(field)
            if eid is None or val is None:
                continue
            out[eid] = out.get(eid, 0.0) + float(val)
    return out


def routed_departures_by_edge(routed_path: str, begin: float, end: float) -> dict[str, int]:
    """Preliminary 'loaded' count per edge: vehicles in the routed file whose depart time
    is in [begin, end), keyed by the first edge of their route. No congestion effects."""
    out: dict[str, int] = {}
    depart = None
    with open(routed_path, encoding="utf-8") as fh:
        for line in fh:
            mv = re.search(r"<vehicle\b[^>]*\bdepart=\"([0-9.]+)\"", line)
            if mv:
                depart = float(mv.group(1))
            mr = re.search(r"<route\b[^>]*\bedges=\"([^\" ]+)", line)
            if mr and depart is not None and begin <= depart < end:
                first = mr.group(1)
                out[first] = out.get(first, 0) + 1
                depart = None
    return out


def compare(observed: dict[str, float], sim: dict[str, float], min_count: float = 0.0) -> list[dict]:
    """Join observed and simulated per-edge counts into comparison rows, sorted by
    observed volume (desc). Only edges whose observed count >= min_count are included."""
    rows: list[dict] = []
    for edge, obs in observed.items():
        if obs < min_count:
            continue
        mod = float(sim.get(edge, 0.0))
        diff = mod - obs
        pct = (diff / obs * 100.0) if obs else None
        rows.append(
            {
                "edge": edge,
                "observed": float(obs),
                "sim": mod,
                "diff": diff,
                "pct": pct,
                "geh": geh(mod, obs),
            }
        )
    rows.sort(key=lambda r: r["observed"], reverse=True)
    return rows


def summarise(rows: list[dict]) -> dict:
    """Aggregate validation quality across comparison rows."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "total_obs": 0.0, "total_sim": 0.0, "mean_abs_pct_err": None,
                "pct_geh_lt5": None, "pct_geh_lt10": None}
    total_obs = sum(r["observed"] for r in rows)
    total_sim = sum(r["sim"] for r in rows)
    pcts = [abs(r["pct"]) for r in rows if r["pct"] is not None]
    lt5 = sum(1 for r in rows if r["geh"] < 5.0)
    lt10 = sum(1 for r in rows if r["geh"] < 10.0)
    return {
        "n": n,
        "total_obs": total_obs,
        "total_sim": total_sim,
        "mean_abs_pct_err": (sum(pcts) / len(pcts)) if pcts else None,
        "pct_geh_lt5": lt5 / n * 100.0,
        "pct_geh_lt10": lt10 / n * 100.0,
    }


def format_report(rows: list[dict], summary: dict, source: str, top: int | None) -> str:
    """Render a Markdown table + summary line for the report."""
    shown = rows[:top] if top else rows
    lines = [
        f"**Baseline validation against real counts** (simulated source: {source})",
        "",
        "| Edge | Observed (veh) | Simulated (veh) | Diff | % | GEH |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in shown:
        pct = "—" if r["pct"] is None else f"{r['pct']:+.0f}%"
        lines.append(
            f"| {r['edge']} | {r['observed']:.0f} | {r['sim']:.0f} | "
            f"{r['diff']:+.0f} | {pct} | {r['geh']:.1f} |"
        )
    s = summary
    lines += [
        "",
        f"Links compared: **{s['n']}**  ·  "
        f"total observed **{s['total_obs']:.0f}** vs simulated **{s['total_sim']:.0f}** "
        f"({(s['total_sim'] - s['total_obs']) / s['total_obs'] * 100.0:+.1f}%)"
        if s["n"] else "No links compared.",
    ]
    if s["n"]:
        lines.append(
            f"GEH < 5: **{s['pct_geh_lt5']:.0f}%** of links  ·  "
            f"GEH < 10: **{s['pct_geh_lt10']:.0f}%**  ·  "
            f"mean |error|: **{s['mean_abs_pct_err']:.0f}%**"
            + ("  (target: GEH < 5 for >= 85% of links)" )
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate the baseline simulation against the real counts.")
    p.add_argument("--flows", default="data/output/demand/traffic_trips.rou.xml",
                   help="Count-derived flow file (observed demand). Default: %(default)s")
    p.add_argument("--sim", help="SUMO edgeData output XML (simulated throughput). Preferred.")
    p.add_argument("--routed", help="Routed demand XML for a preliminary loading check (no congestion).")
    p.add_argument("--begin", type=float, default=DEFAULT_BEGIN, help="Window start, sim seconds (default 23400 = 06:30).")
    p.add_argument("--end", type=float, default=DEFAULT_END, help="Window end, sim seconds (default 27000 = 07:30).")
    p.add_argument("--field", default=DEFAULT_FIELD, help="edgeData field to sum (default: departed).")
    p.add_argument("--min-count", type=float, default=0.0, help="Ignore edges with observed < this.")
    p.add_argument("--top", type=int, default=None, help="Show only the top N edges by observed volume.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    observed = observed_by_edge(args.flows, args.begin, args.end)
    if args.sim:
        sim = sim_by_edge(args.sim, args.begin, args.end, args.field)
        source = f"SUMO edgeData '{args.field}'"
    elif args.routed:
        sim = {k: float(v) for k, v in routed_departures_by_edge(args.routed, args.begin, args.end).items()}
        source = "routed demand (loading only; no congestion)"
    else:
        print("error: pass --sim <edgedata.xml> (preferred) or --routed <routed.rou.xml>", file=sys.stderr)
        return 2
    rows = compare(observed, sim, args.min_count)
    print(format_report(rows, summarise(rows), source, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
