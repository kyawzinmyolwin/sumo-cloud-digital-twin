"""
Run the full Christchurch Central City SUMO pipeline (network + demand).

Prefer separate scripts for faster iteration on traffic CSV:
  python create_network.py   # network, map (slow — run when OSM/network changes)
  python create_demand.py    # trips, duarouter (fast — rerun for new CSV)

This script runs both in one process (same as before the split).

Usage:
  python scripts/build_simulation.py
  python scripts/build_simulation.py --skip-network
  python scripts/build_simulation.py --traffic data/output/demand/traffic_18MAY2026_130842.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sim_pipeline import (
    ALL_STEPS,
    SUMOCFG,
    add_common_args,
    parse_steps,
    run_pipeline,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run the full Christchurch Central City SUMO pipeline.",
    )
    add_common_args(p, step_choices=ALL_STEPS)
    p.add_argument(
        "--skip-network",
        action="store_true",
        help="Skip network build (use existing .net.xml)",
    )
    p.add_argument(
        "--skip-map",
        action="store_true",
        help="Skip intersection to edge mapping",
    )
    p.add_argument(
        "--skip-trips",
        action="store_true",
        help="Skip traffic CSV to flows",
    )
    p.add_argument(
        "--no-duarouter",
        action="store_true",
        help="Do not run duarouter after building trips",
    )
    p.add_argument(
        "--traffic",
        type=Path,
        metavar="CSV",
        help="Traffic CSV (default: newest data/output/demand/traffic_*.csv)",
    )
    p.add_argument(
        "--routing-threads",
        type=int,
        metavar="N",
        help="duarouter: use N parallel routing threads",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skip = set()
    if args.skip_network:
        skip.add("network")
    if args.skip_map:
        skip.add("map")
    if args.skip_trips:
        skip.add("trips")
    if args.no_duarouter:
        skip.add("duarouter")
    try:
        steps = parse_steps(args.only, skip, allowed=ALL_STEPS)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2

    return run_pipeline(
        args,
        steps,
        done_message="COMPLETED: pipeline",
        run_hint=(
            "Done. Run simulation, e.g.\n"
            f"  sumo-gui -c {SUMOCFG.name}\n"
            "Tip: use scripts/create_network.py + scripts/create_demand.py to avoid rebuilding "
            "the network on every traffic CSV run."
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
