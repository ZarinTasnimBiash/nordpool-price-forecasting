"""Nord Pool Dagster assets/jobs/schedules (reference: nordpool_assets.py).

Partitioning: daily by *delivery date*, with end_offset=2 so tomorrow's
partition exists today — because tomorrow's prices are published ~13:00 CET
today (this mirrors the reference platform's "day-ahead offset logic"). end_offset=1
would only make *today's* partition exist, leaving the schedule a day behind.
Schedule: daily at 13:30 Europe/Stockholm, right after auction publication.
"""

from __future__ import annotations

import pandas as pd
from dagster import (DailyPartitionsDefinition, RetryPolicy, asset,
                     build_schedule_from_partitioned_job, define_asset_job)

from config.configs import NORDPOOL_HISTORY_START
from ingestion.nordpool import nordpool_data
from orchestration.utils.local_save_asset import build_save_assets

GROUP = "nordpool"
RETRY = RetryPolicy(max_retries=3)

# Starts inside the public API's rolling ~63-day window, not at HISTORY_START:
# older partitions could never be materialized (401). Training history comes
# from the elpriset backfill instead (CLI only).
daily_partitions = DailyPartitionsDefinition(
    start_date=NORDPOOL_HISTORY_START, end_offset=2, timezone="Europe/Stockholm",
)

ENDPOINTS = ["day_ahead_prices"]  # volumes dropped: endpoint 404s (see configs.py)

assets = []
for _endpoint in ENDPOINTS:

    def _make_get(endpoint):
        @asset(name=f"get_{endpoint}_daily", partitions_def=daily_partitions,
               group_name=GROUP, retry_policy=RETRY)
        def get_endpoint_daily(context) -> pd.DataFrame:
            day = context.partition_key
            df = nordpool_data.collect(endpoint, day, day, write=False)
            context.log.info("fetched %s rows for %s %s", len(df), endpoint, day)
            return df

        return get_endpoint_daily

    assets.append(_make_get(_endpoint))
    assets.extend(build_save_assets(_endpoint, "nordpool", "daily",
                                    daily_partitions, GROUP))

nordpool_job = define_asset_job(
    "nordpool_job",
    selection=[a.key for a in assets],
)

# 13:30 local time: day-ahead auction results are published ~13:00 CET
nordpool_schedule = build_schedule_from_partitioned_job(
    nordpool_job, hour_of_day=13, minute_of_hour=30,
)

jobs = [nordpool_job]
schedules = [nordpool_schedule]
