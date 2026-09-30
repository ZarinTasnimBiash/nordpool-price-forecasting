"""SMHI weather Dagster assets (reference: weather_smhi_assets.py).

Mirrors the reference platform's design exactly, including the backfill guard: the SMHI
latest-day endpoint only serves the most recent day, so materializing any
partition other than the latest raises Failure ("latest partition only").
Historical depth comes from the separate archive backfill in the CLI.
"""

from __future__ import annotations

import pandas as pd
from dagster import (DailyPartitionsDefinition, Failure, RetryPolicy, asset,
                     build_schedule_from_partitioned_job, define_asset_job)

from config.configs import HISTORY_START
from ingestion.smhi import smhi_data
from orchestration.utils.local_save_asset import build_save_assets

GROUP = "weather_data"
RETRY = RetryPolicy(max_retries=3)

daily_partitions = DailyPartitionsDefinition(
    start_date=HISTORY_START, timezone="Europe/Stockholm",
)


@asset(name="get_weather_observations_daily", partitions_def=daily_partitions,
       group_name=GROUP, retry_policy=RETRY)
def get_weather_observations_daily(context) -> pd.DataFrame:
    # Backfill guard (reference pattern): reject non-latest partitions, because
    # the SMHI latest-day endpoint always returns only the latest day.
    latest = daily_partitions.get_last_partition_key()
    if context.partition_key != latest:
        raise Failure(
            f"SMHI latest-day endpoint cannot backfill: partition "
            f"{context.partition_key} != latest {latest}. Use "
            f"`python -m ingestion.main --api smhi --mode archive --write`."
        )
    df = smhi_data.collect(mode="latest", write=False)
    context.log.info("fetched %s weather rows", len(df))
    return df


assets = [get_weather_observations_daily]
assets.extend(build_save_assets("weather_observations", "smhi", "daily",
                                daily_partitions, GROUP))

weather_smhi_job = define_asset_job(
    "weather_smhi_job",
    selection=[a.key for a in assets],
)

# Daily at 00:30 local (weather jobs run just after midnight; shifted slightly
# so the previous day's observations are complete)
weather_smhi_schedule = build_schedule_from_partitioned_job(
    weather_smhi_job, hour_of_day=0, minute_of_hour=30,
)

jobs = [weather_smhi_job]
schedules = [weather_smhi_schedule]
