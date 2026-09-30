"""Manual execution CLI (reference pattern: api/main.py --api <source> ...).

Examples
--------
Backfill ~3 years of historical day-ahead prices (elprisetjustnu.se):
    python -m ingestion.main --api elpriset \
        --start_date 2023-09-01 --end_date 2026-08-19 --write

Fetch recent Nord Pool day-ahead prices (public API only serves ~63 days):
    python -m ingestion.main --api nordpool --endpoint day_ahead_prices \
        --start_date 2026-06-17 --end_date 2026-08-19 --write

Backfill archived weather forecasts (what was predicted, not what happened):
    python -m ingestion.main --api openmeteo --write

Backfill all SMHI weather history from the corrected archive:
    python -m ingestion.main --api smhi --mode archive --write

Fetch latest-day weather (what the daily Dagster job runs):
    python -m ingestion.main --api smhi --mode latest --write

Validate configuration (API reachability + SMHI station/parameter combos):
    python -m ingestion.main --validate
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ingestion.main")


def run_validate() -> int:
    from ingestion.nordpool import nordpool_data
    from ingestion.smhi import smhi_data

    ok = True
    probe_day = (date.today() - timedelta(days=2)).isoformat()
    print(f"[1/2] Nord Pool API probe (day-ahead prices, {probe_day}) ...")
    try:
        payload = nordpool_data.fetch_endpoint("day_ahead_prices", probe_day)
        n = len(payload.get("multiAreaEntries", [])) if payload else 0
        print(f"      OK — {n} delivery periods returned")
        if payload:
            rows = nordpool_data.normalize_prices(payload)
            print(f"      normalized to {len(rows)} rows "
                  f"(sample: {rows[0] if rows else 'n/a'})")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"      FAILED: {e}")

    print("[2/2] SMHI station/parameter combos ...")
    problems = smhi_data.validate_stations()
    if problems:
        ok = False
        for p in problems:
            print(f"      PROBLEM: {p}")
    else:
        print("      OK — all 24 (area x parameter) combos exist and are active")
    print("\nValidation " + ("PASSED" if ok else "FAILED (see problems above)"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manual/backfill data collection")
    ap.add_argument("--api", choices=["nordpool", "elpriset", "smhi", "openmeteo"])
    ap.add_argument("--endpoint", default=None,
                    help="nordpool endpoint (default: day_ahead_prices)")
    ap.add_argument("--start_date", help="YYYY-MM-DD (nordpool, elpriset)")
    ap.add_argument("--end_date", help="YYYY-MM-DD (nordpool, elpriset)")
    ap.add_argument("--mode", choices=["latest", "recent", "archive"],
                    default="latest",
                    help="smhi collection mode: latest day / last ~4 months / "
                         "full corrected archive")
    ap.add_argument("--write", action="store_true",
                    help="persist to parquet + DuckDB (otherwise dry run)")
    ap.add_argument("--validate", action="store_true",
                    help="validate API access and station config, then exit")
    args = ap.parse_args(argv)

    if args.validate:
        return run_validate()
    if not args.api:
        ap.error("--api is required (or use --validate)")

    if args.api == "nordpool":
        from ingestion.nordpool import nordpool_data
        if not (args.start_date and args.end_date):
            ap.error("nordpool requires --start_date and --end_date")
        endpoint = args.endpoint or "day_ahead_prices"
        df = nordpool_data.collect(endpoint, args.start_date, args.end_date,
                                   write=args.write)
        print(f"collected {len(df)} rows for {endpoint} "
              f"({'written' if args.write else 'dry run'})")
    elif args.api == "openmeteo":
        from ingestion.openmeteo import openmeteo_data
        from config.configs import FORECAST_HISTORY_START
        df = openmeteo_data.collect(args.start_date or FORECAST_HISTORY_START,
                                    args.end_date, write=args.write)
        print(f"collected {len(df)} forecast rows "
              f"({'written' if args.write else 'dry run'})")
    elif args.api == "elpriset":
        from ingestion.elpriset import elpriset_data
        if not (args.start_date and args.end_date):
            ap.error("elpriset requires --start_date and --end_date")
        df = elpriset_data.collect(args.start_date, args.end_date,
                                   write=args.write)
        print(f"collected {len(df)} elpriset price rows "
              f"({'written' if args.write else 'dry run'})")
    else:
        from ingestion.smhi import smhi_data
        df = smhi_data.collect(mode=args.mode, write=args.write)
        print(f"collected {len(df)} weather rows "
              f"({'written' if args.write else 'dry run'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
