#!/usr/bin/env python3
"""
Fetch one survey date's CCC counts from the ETL store (S3) and write the CSV that
make_cbd_scenario.py / sumo_demand_from_traffic_csv.py consume.

This is the thin AWS wrapper around etl.scenario_bridge (which is pure/testable).
It turns "the ETL already grabbed the counts from the CCC Drive" into "here is a
ready-to-simulate CSV for date X", so the whole path is:

    # 1. bridge: ETL/S3 counts for a date -> CSV
    python scripts/fetch_counts_for_date.py --date 2025-08-13 \\
        --bucket <etl-data-bucket> --output scenarios/counts_2025-08-13.csv

    # 2. build + play the CBD scenario from that CSV (period/time window here)
    python scripts/make_cbd_scenario.py --name am_2025 \\
        --traffic-csv scenarios/counts_2025-08-13.csv --start-time 07:00 --end-time 09:00
    python scripts/run_traci.py --sumocfg scenarios/am_2025.sumocfg \\
        --no-gui --emit --emit-host 0.0.0.0 --jump-to 0

The bucket is the ETL data bucket (terraform output ``etl_data_bucket``); pass it
with --bucket or set ETL_DATA_BUCKET. Needs AWS credentials (the VM/EC2 has them).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from etl.s3_store import S3ObjectStore  # noqa: E402
from etl.scenario_bridge import (  # noqa: E402
    available_dates, build_counts_csv_for_date, PROCESSED_PREFIX,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a date's CCC counts from the ETL store -> CSV.")
    ap.add_argument("--date", help="Survey date to fetch (YYYY-MM-DD).")
    ap.add_argument("--output", type=Path, help="Output CSV path (required unless --list-dates).")
    ap.add_argument("--list-dates", action="store_true",
                    help="List the survey dates the ETL has ingested (with #intersections) and exit.")
    ap.add_argument("--bucket", default=os.environ.get("ETL_DATA_BUCKET"),
                    help="ETL data bucket (or set ETL_DATA_BUCKET). terraform output etl_data_bucket.")
    ap.add_argument("--prefix", default=PROCESSED_PREFIX,
                    help=f"Processed-data key prefix (default: {PROCESSED_PREFIX}).")
    args = ap.parse_args()

    if not args.bucket:
        print("Error: no bucket given. Pass --bucket or set ETL_DATA_BUCKET "
              "(terraform output etl_data_bucket).", file=sys.stderr)
        return 2

    store = S3ObjectStore(args.bucket)

    if args.list_dates:
        dates = available_dates(store, prefix=args.prefix)
        if not dates:
            print(f"No dates ingested yet in s3://{args.bucket}/{args.prefix}/. "
                  f"Run the ETL ingest Lambda to populate it.", file=sys.stderr)
            return 1
        print(f"{len(dates)} survey date(s) ingested (date: #intersections):")
        for d in sorted(dates):
            print(f"  {d}: {dates[d]}")
        return 0

    if not args.date or not args.output:
        print("Error: --date and --output are required (unless --list-dates).", file=sys.stderr)
        return 2

    summary = build_counts_csv_for_date(store, args.date, args.output, prefix=args.prefix)

    if summary["rows"] == 0:
        print(f"Warning: no counts found for {args.date} in s3://{args.bucket}/{args.prefix}/. "
              f"Has the ETL ingested this date? (Only surveyed days exist.)", file=sys.stderr)
        return 1

    slots = summary["time_slots"]
    span = f"{slots[0]}..{slots[-1]}" if slots else "(no time slots)"
    print(f"wrote {summary['rows']} rows from {summary['intersections']} intersections "
          f"-> {summary['output']}")
    print(f"time-slot coverage: {span}  ({len(slots)} slots)")
    print("\nnext: build a CBD scenario from it (pick a peak window):")
    print(f"  python scripts/make_cbd_scenario.py --name <name> "
          f"--traffic-csv {summary['output']} --start-time 07:00 --end-time 09:00")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
