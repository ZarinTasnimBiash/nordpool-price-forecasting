"""Nord Pool Data Portal collection: fetch + normalize day-ahead data.

API: https://dataportal-api.nordpoolgroup.com/api/<Endpoint>?date=YYYY-MM-DD
     &market=DayAhead&deliveryArea=SE1,SE2,SE3,SE4&currency=EUR

Behaviour mirrors the reference platform's conventions:
- up to 3 retries with exponential backoff (their RetryPolicy(max_retries=3))
- polite rate limiting between backfill calls
- unexpected payloads are dumped to data/raw/_debug/ instead of crashing
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

import pandas as pd
import requests

from config.configs import BIDDING_AREAS, CURRENCY, STORAGE, api_config
from ingestion.data_handler import DataHandler, utcnow
from schemas.utils import schema_mapper

logger = logging.getLogger(__name__)

CFG = api_config["nordpool"]


# --------------------------------------------------------------------- fetch
def fetch_endpoint(endpoint: str, day: str, areas=None, max_retries: int = 3):
    """Fetch one endpoint for one delivery date. Returns dict payload or None.

    Returns None for 204 (no data published for that date yet).
    """
    ep = CFG["endpoints"][endpoint]
    params = {
        "date": day,
        "deliveryArea": ",".join(areas or BIDDING_AREAS),
        "currency": CURRENCY,
        **ep["params"],
    }
    url = f"{CFG['base_url']}/{ep['path']}"
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=CFG["headers"], timeout=30)
            if r.status_code == 204:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - logged and retried
            last_err = e
            wait = 2**attempt
            logger.warning("fetch %s %s attempt %s failed (%s); retry in %ss",
                           endpoint, day, attempt, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"nordpool fetch failed for {endpoint} {day}: {last_err}")


# ----------------------------------------------------------------- normalize
def _resolution_minutes(start: pd.Timestamp, end: pd.Timestamp) -> int:
    return int((end - start).total_seconds() // 60)


def normalize_prices(payload: dict) -> list[dict]:
    """multiAreaEntries -> one row per (delivery period, area)."""
    rows = []
    updated = payload.get("updatedAt")
    ddate = payload.get("deliveryDateCET", "")
    currency = payload.get("currency", CURRENCY)
    for entry in payload.get("multiAreaEntries", []):
        start = pd.Timestamp(entry["deliveryStart"])
        end = pd.Timestamp(entry["deliveryEnd"])
        for area, price in (entry.get("entryPerArea") or {}).items():
            rows.append({
                "delivery_start_utc": start,
                "delivery_end_utc": end,
                "delivery_area": area,
                "price_eur_mwh": price,
                "currency": currency,
                "resolution_minutes": _resolution_minutes(start, end),
                "delivery_date_cet": ddate,
                "updated_at_utc": updated,
            })
    return rows


def normalize_volumes(payload: dict) -> list[dict]:
    """Defensive normalizer: entryPerArea values may be numbers or dicts."""
    rows = []
    updated = payload.get("updatedAt")
    ddate = payload.get("deliveryDateCET", "")
    for entry in payload.get("multiAreaEntries", []):
        start = pd.Timestamp(entry["deliveryStart"])
        end = pd.Timestamp(entry["deliveryEnd"])
        for area, val in (entry.get("entryPerArea") or {}).items():
            row = {
                "delivery_start_utc": start,
                "delivery_end_utc": end,
                "delivery_area": area,
                "volume_buy_mwh": None,
                "volume_sell_mwh": None,
                "resolution_minutes": _resolution_minutes(start, end),
                "delivery_date_cet": ddate,
                "updated_at_utc": updated,
            }
            if isinstance(val, dict):
                # try common key spellings
                row["volume_buy_mwh"] = val.get("buy", val.get("buyVolume"))
                row["volume_sell_mwh"] = val.get("sell", val.get("sellVolume"))
            else:  # single total volume number
                row["volume_buy_mwh"] = val
            rows.append(row)
    return rows


_NORMALIZERS = {
    "day_ahead_prices": normalize_prices,
    "day_ahead_volumes": normalize_volumes,
}


def _dump_debug(endpoint: str, day: str, payload: dict, err: Exception):
    d = STORAGE["raw_dir"] / "_debug"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"nordpool_{endpoint}_{day}.json"
    p.write_text(json.dumps(payload, indent=2, default=str))
    logger.error("normalize failed for %s %s (%s) — raw payload dumped to %s",
                 endpoint, day, err, p)


# ------------------------------------------------------------------- collect
def collect(endpoint: str, start_date: str, end_date: str, write: bool = False,
            progress_every: int = 30) -> pd.DataFrame:
    """Fetch a date range (inclusive) and optionally persist per-day partitions."""
    handler = DataHandler(schema_mapper[endpoint], "nordpool", endpoint)
    normalize = _NORMALIZERS[endpoint]
    d0, d1 = date.fromisoformat(start_date), date.fromisoformat(end_date)
    frames, day, n_days = [], d0, (d1 - d0).days + 1
    fetched = 0
    while day <= d1:
        day_str = day.isoformat()
        payload = fetch_endpoint(endpoint, day_str)
        if payload is not None:
            try:
                rows = normalize(payload)
            except Exception as e:  # noqa: BLE001
                _dump_debug(endpoint, day_str, payload, e)
                rows = []
            if rows:
                now = utcnow()
                for r in rows:
                    r.setdefault("updated_at_utc", now)
                if write:
                    frames.append(handler.persist(rows, day_str))
                else:
                    frames.append(handler.to_dataframe(rows))
        fetched += 1
        if fetched % progress_every == 0:
            logger.info("nordpool %s: %s/%s days done", endpoint, fetched, n_days)
        time.sleep(CFG["rate_limit_seconds"])
        day += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
