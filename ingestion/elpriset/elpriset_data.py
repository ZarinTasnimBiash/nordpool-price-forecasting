"""elprisetjustnu.se collection: historical day-ahead prices per bidding area.

Why this source exists alongside Nord Pool: the public Nord Pool Data Portal
only serves a rolling ~63-day window, so it cannot provide training history.
This service republishes the same day-ahead prices back to 2021, free and
without an account. Nord Pool stays the live daily source; this one backfills.

Same conventions as the other sources: up to 3 retries with exponential
backoff, polite rate limiting, one row per (delivery period, area).
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd
import requests

from config.configs import BIDDING_AREAS, api_config
from ingestion.data_handler import DataHandler, utcnow
from schemas.utils import schema_mapper

logger = logging.getLogger(__name__)

CFG = api_config["elpriset"]
ENDPOINT = "elpriset_day_ahead_prices"


# --------------------------------------------------------------------- fetch
def fetch_day(day: str, area: str, max_retries: int = 3) -> list[dict] | None:
    """Fetch one (day, area). Returns list payload, or None if not published."""
    d = date.fromisoformat(day)
    url = f"{CFG['base_url']}/{d.year}/{d.month:02d}-{d.day:02d}_{area}.json"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=CFG["headers"], timeout=30)
            if r.status_code == 404:
                return None  # no prices published for that day/area
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - logged and retried
            last_err = e
            wait = 2**attempt
            logger.warning("fetch elpriset %s %s attempt %s failed (%s); retry in %ss",
                           area, day, attempt, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"elpriset fetch failed for {area} {day}: {last_err}")


# ----------------------------------------------------------------- normalize
def normalize(payload: list[dict], area: str) -> list[dict]:
    """Payload records -> one row per delivery period.

    `time_start`/`time_end` carry a local offset (+01:00 / +02:00), so parsing
    them is DST-correct; converted to UTC for storage. Prices arrive per kWh
    and are converted to EUR/MWh to match the Nord Pool schema.
    """
    rows = []
    now = utcnow()
    for rec in payload:
        start = pd.Timestamp(rec["time_start"]).tz_convert("UTC")
        end = pd.Timestamp(rec["time_end"]).tz_convert("UTC")
        rows.append({
            "delivery_start_utc": start,
            "delivery_end_utc": end,
            "delivery_area": area,
            "price_eur_mwh": rec["EUR_per_kWh"] * 1000.0,
            "price_sek_kwh": rec["SEK_per_kWh"],
            "exchange_rate": rec["EXR"],
            "resolution_minutes": int((end - start).total_seconds() // 60),
            # the local calendar date is the auction's delivery date
            "delivery_date_cet": pd.Timestamp(rec["time_start"]).date().isoformat(),
            "inserted_at_utc": now,
        })
    return rows


# ------------------------------------------------------------------- collect
def collect(start_date: str, end_date: str, areas=None, write: bool = False,
            progress_every: int = 30) -> pd.DataFrame:
    """Fetch a date range (inclusive) across areas, optionally persisting."""
    handler = DataHandler(schema_mapper[ENDPOINT], "elpriset", ENDPOINT)
    d0, d1 = date.fromisoformat(start_date), date.fromisoformat(end_date)
    areas = areas or BIDDING_AREAS
    frames, day, n_days = [], d0, (d1 - d0).days + 1
    fetched, missing = 0, 0
    while day <= d1:
        day_str = day.isoformat()
        day_rows: list[dict] = []
        for area in areas:
            payload = fetch_day(day_str, area)
            if payload:
                day_rows.extend(normalize(payload, area))
            else:
                missing += 1
            time.sleep(CFG["rate_limit_seconds"])
        if day_rows:
            if write:
                frames.append(handler.persist(day_rows, day_str))
            else:
                frames.append(handler.to_dataframe(day_rows))
        fetched += 1
        if fetched % progress_every == 0:
            logger.info("elpriset: %s/%s days done (%s area-days missing)",
                        fetched, n_days, missing)
        day += timedelta(days=1)
    if missing:
        logger.warning("elpriset: %s (day, area) combinations had no data", missing)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
