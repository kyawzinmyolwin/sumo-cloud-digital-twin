"""
Build the SUMO network and intersection-to-edge map (run once, or when OSM/network changes).

Steps:
  network — filter OSM to main streets, netconvert, TLS rebuild
  map     — data/output/intersection_geo.csv -> data/output/network/intersection_to_edges.csv

Usage:
  python scripts/create_network.py
  python scripts/create_network.py --only map
  python scripts/create_network.py --skip-network
"""
from __future__ import annotations

import argparse
import sys

from sim_pipeline import (
    NETWORK_STEPS,
    add_common_args,
    parse_steps,
    run_pipeline,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build Christchurch Central City SUMO network and intersection map.",
    )
    add_common_args(p, step_choices=NETWORK_STEPS)
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skip = set()
    if args.skip_network:
        skip.add("network")
    if args.skip_map:
        skip.add("map")
    try:
        steps = parse_steps(args.only, skip, allowed=NETWORK_STEPS)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2

    return run_pipeline(
        args,
        steps,
        done_message="COMPLETED: network pipeline",
        run_hint=(
            "Next: build demand and routes, e.g.\n"
            "  python scripts/create_demand.py"
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
