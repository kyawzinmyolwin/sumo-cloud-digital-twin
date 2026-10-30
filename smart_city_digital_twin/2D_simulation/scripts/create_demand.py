"""
Build traffic demand and routes from a traffic CSV (fast iteration).

Requires outputs from create_network.py:
  data/output/network/Christchurch_Central_City_main_streets.net.xml
  data/output/network/intersection_to_edges.csv

Steps:
  trips     — data/output/demand/traffic_*.csv -> data/output/demand/traffic_trips.rou.xml
  duarouter — data/output/demand/traffic_trips.rou.xml -> data/output/demand/traffic_trips.routed.rou.xml

Usage:
  python scripts/create_demand.py
  python scripts/create_demand.py --only trips
  python scripts/create_demand.py --traffic data/output/demand/traffic_18MAY2026_130842.csv --no-duarouter
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sim_pipeline import (
    DEMAND_STEPS,
    NET_XML,
    OUT_ROUTED,
    add_common_args,
    parse_steps,
    preflight_routed_demand,
    run_pipeline,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Build demand and routes from traffic CSV "
            "(uses existing network from create_network.py)."
        ),
    )
    add_common_args(p, step_choices=DEMAND_STEPS)
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
    p.add_argument(
        "--trip-workers",
        type=int,
        default=0,
        metavar="N",
        help="Parallel workers for trip edge resolution (0=auto, usually CPU count - 1)",
    )
    p.add_argument(
        "--embed-trip-routes",
        action="store_true",
        help="Embed <route> in trips XML even when duarouter runs (slower trips step)",
    )
    p.add_argument(
        "--no-cordon-calibrate",
        action="store_true",
        help=(
            "Do not scale boundary source/sink cars to CCC SmartView cordon volume "
            f"({225_000:,}/day), M-curve hourly profile, and directional shares"
        ),
    )
    p.add_argument(
        "--no-bus-interchange-calibrate",
        action="store_true",
        help=(
            "Do not cap bus demand to ECan interchange hourly profile "
            "(96/h ceiling; ~1,250 daily stops; AM/PM peaks ~80/h)"
        ),
    )
    p.add_argument(
        "--check-routes",
        action="store_true",
        help=(
            "Only validate data/output/demand/traffic_trips.routed.rou.xml against the current net "
            "(no trips/duarouter); exit 1 if any invalid hop is found"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_routes:
        if not NET_XML.is_file():
            print("missing:", NET_XML, file=sys.stderr)
            return 1
        if not OUT_ROUTED.is_file():
            print("missing:", OUT_ROUTED, file=sys.stderr)
            return 1
        checked, bad_n, samples = preflight_routed_demand()
        if bad_n:
            print(
                f"FAIL: {bad_n} vehicle(s) with invalid consecutive edges "
                f"(checked {checked:,}) in {OUT_ROUTED.name}",
                file=sys.stderr,
            )
            for vid, a, b in samples:
                print(f"  {vid}: {a} -> {b}", file=sys.stderr)
            print(
                "Re-run: python create_network.py  (internal bicycle lanes)\n"
                "       python create_demand.py",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK: {checked:,} vehicle route(s) validate against {NET_XML.name}"
        )
        return 0
    args.cordon_calibrate = not args.no_cordon_calibrate
    args.bus_interchange_calibrate = not args.no_bus_interchange_calibrate
    skip = set()
    if args.skip_trips:
        skip.add("trips")
    if args.no_duarouter:
        skip.add("duarouter")
    try:
        steps = parse_steps(args.only, skip, allowed=DEMAND_STEPS)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2

    if not NET_XML.is_file():
        print("missing:", NET_XML, file=sys.stderr)
        print("Run create_network.py first.", file=sys.stderr)
        return 1

    return run_pipeline(
        args,
        steps,
        done_message="COMPLETED: demand pipeline",
        run_hint=(
            "Done. Run simulation, e.g.\n"
            f"  sumo-gui -n {NET_XML.name} -r {OUT_ROUTED.name}"
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
