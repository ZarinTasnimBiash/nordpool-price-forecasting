"""Global Dagster registry (reference pattern: one definitions.py wires it all).

This file is the single place that determines what appears in the Dagster UI
and what the daemon can execute. Run locally with:

    dagster dev -f orchestration/definitions.py
"""

from dagster import Definitions

from orchestration import nordpool_assets, weather_smhi_assets

assets = [*nordpool_assets.assets, *weather_smhi_assets.assets]
jobs = [*nordpool_assets.jobs, *weather_smhi_assets.jobs]
schedules = [*nordpool_assets.schedules, *weather_smhi_assets.schedules]

defs = Definitions(assets=assets, jobs=jobs, schedules=schedules)
