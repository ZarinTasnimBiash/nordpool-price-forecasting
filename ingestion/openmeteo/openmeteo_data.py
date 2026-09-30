"""Open-Meteo previous-runs collection: what the weather was FORECAST to be.

Why this source exists. SMHI reports what the weather actually did, which is
only knowable after the fact. At the 11:00 cutoff the task is forecasting tomorrow,
and tomorrow's observations do not exist yet — so observation-based weather
features can only ever describe the past. A forecast published *before* the
cutoff is different: it genuinely was available, so using it is not leakage.

This module fetches the archive of those past forecasts, keyed by how old the
forecast was (`lead_days`), so a feature can say "what was believed about
tomorrow, yesterday" rather than "what turned out to be true".

One request covers a whole date range for one area, so the backfill is a
handful of calls rather than thousands.
"""

from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd
import requests

from config.configs import BIDDING_AREAS, FORECAST_HISTORY_START, api_config
from ingestion.data_handler import DataHandler, utcnow
from schemas.utils import schema_mapper

logger = logging.getLogger(__name__)

CFG = api_config["openmeteo"]
ENDPOINT = "weather_forecast"
CHUNK_DAYS = 200          # keep each response a manageable size


def _hourly_fields() -> list[str]:
    """Every variable needed, at every lead time including the analysed value."""
    fields = []
    for var in CFG["variables"]:
        fields.append(var)                                    # lead_days = 0
        for lead in CFG["lead_days"]:
            fields.append(f"{var}_previous_day{lead}")
    return fields


# --------------------------------------------------------------------- fetch
def fetch_range(area: str, start: str, end: str, max_retries: int = 3) -> dict:
    """One request: all variables, all lead times, for a date range."""
    point = CFG["points"][area]
    params = {
        "latitude": point["lat"], "longitude": point["lon"],
        "start_date": start, "end_date": end,
        "hourly": ",".join(_hourly_fields()),
        "timezone": "UTC",
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(CFG["base_url"], params=params,
                             headers=CFG["headers"], timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - logged and retried
            last_err = e
            wait = 2**attempt
            logger.warning("openmeteo %s %s..%s attempt %s failed (%s); retry in %ss",
                           area, start, end, attempt, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"openmeteo fetch failed for {area} {start}..{end}: {last_err}")


# ----------------------------------------------------------------- normalize
def normalize(payload: dict, area: str) -> list[dict]:
    """Wide hourly arrays -> one row per (hour, parameter, lead_days)."""
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return []
    stamps = pd.to_datetime(pd.Series(times), utc=True)
    now = utcnow()

    rows = []
    for var, param in CFG["variables"].items():
        for lead in [0, *CFG["lead_days"]]:
            key = var if lead == 0 else f"{var}_previous_day{lead}"
            values = hourly.get(key)
            if not values:
                continue
            for t, v in zip(stamps, values):
                if v is None:
                    continue
                rows.append({
                    "time_utc": t,
                    "area": area,
                    "parameter": param,
                    "lead_days": lead,
                    "value": float(v),
                    "inserted_at_utc": now,
                })
    return rows


# ------------------------------------------------------------------- collect
def collect(start_date: str = FORECAST_HISTORY_START, end_date: str | None = None,
            areas=None, write: bool = False) -> pd.DataFrame:
    """Fetch archived forecasts for every area over a date range."""
    handler = DataHandler(schema_mapper[ENDPOINT], "openmeteo", ENDPOINT)
    end_date = end_date or date.today().isoformat()
    areas = areas or BIDDING_AREAS

    # Loop dates on the OUTSIDE and areas on the inside, so one write covers
    # every area for a time range. DataHandler.load_duckdb replaces by
    # timestamp range alone, so writing one area at a time would delete the
    # areas written before it.
    frames = []
    cursor = date.fromisoformat(start_date)
    stop = date.fromisoformat(end_date)
    while cursor <= stop:
        chunk_end = min(stop, cursor + pd.Timedelta(days=CHUNK_DAYS - 1).to_pytimedelta())
        chunk_rows: list[dict] = []
        for area in areas:
            payload = fetch_range(area, cursor.isoformat(), chunk_end.isoformat())
            rows = normalize(payload, area)
            logger.info("openmeteo %s %s..%s -> %s rows",
                        area, cursor, chunk_end, len(rows))
            chunk_rows.extend(rows)
            time.sleep(CFG["rate_limit_seconds"])
        if chunk_rows:
            label = f"{cursor.isoformat()}_{chunk_end.isoformat()}"
            frames.append(handler.persist(chunk_rows, label) if write
                          else handler.to_dataframe(chunk_rows))
        cursor = chunk_end + pd.Timedelta(days=1).to_pytimedelta()

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
