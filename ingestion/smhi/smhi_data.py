"""SMHI Open Data collection: weather observations per bidding area.

Two modes, mirroring the reference platform's SMHI pipeline reality:
- "latest":  the daily pipeline fetch (period/latest-day, JSON) — this is what
             Dagster runs on schedule; it cannot backfill (endpoint only serves
             the latest day), the same constraint the reference platform documents.
- "archive": one-off historical backfill (period/corrected-archive, CSV) —
             the reference platform does this via separate CSV loads; automated here.
"""

from __future__ import annotations

import io
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from config.configs import BIDDING_AREAS, HISTORY_START, api_config, get_station
from ingestion.data_handler import DataHandler, utcnow
from schemas.utils import schema_mapper

logger = logging.getLogger(__name__)

CFG = api_config["smhi"]


def _url(param_id: int, station_id: int, period: str, fmt: str) -> str:
    return (f"{CFG['base_url']}/parameter/{param_id}/station/{station_id}"
            f"/period/{period}/data.{fmt}")


def _get(url: str, max_retries: int = 3) -> requests.Response:
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2**attempt)
    raise RuntimeError(f"smhi fetch failed: {url}: {last_err}")


# ------------------------------------------------------------------- latest
def fetch_latest_day(param_name: str, param_id: int, area: str,
                     station_id: int) -> list[dict]:
    url = _url(param_id, station_id, "latest-day", "json")
    payload = _get(url).json()
    now = utcnow()
    rows = []
    for v in payload.get("value") or []:
        rows.append({
            "time_utc": datetime.fromtimestamp(v["date"] / 1000, tz=timezone.utc),
            "area": area,
            "parameter": param_name,
            "station_id": station_id,
            "value": float(v["value"]),
            "quality": v.get("quality", ""),
            "inserted_at_utc": now,
        })
    return rows


# ------------------------------------------------------------------ archive
def fetch_archive(param_name: str, param_id: int, area: str, station_id: int,
                  since: str, period: str = "corrected-archive") -> list[dict]:
    """Parse an SMHI CSV export (semicolon-separated, metadata header).

    `corrected-archive` holds quality-checked history but lags ~3 months behind
    now; `latest-months` covers that tail. They overlap, so fetching both gives
    continuous coverage. Both use the identical CSV layout.
    """
    url = _url(param_id, station_id, period, "csv")
    text = _get(url).text
    # find the real header line, e.g. "Datum;Tid (UTC);Lufttemperatur;Kvalitet..."
    lines = text.splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("Datum;Tid (UTC)")), None
    )
    if header_idx is None:
        logger.warning("no data header in archive for %s/%s (station %s)",
                       area, param_name, station_id)
        return []
    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=";",
                     usecols=[0, 1, 2, 3],
                     names=["date", "time", "value", "quality"],
                     header=0, dtype=str)
    df = df.dropna(subset=["date", "time", "value"])
    df = df[df["date"] >= since]
    if df.empty:
        return []
    ts = pd.to_datetime(df["date"] + " " + df["time"], utc=True)
    now = utcnow()
    return [
        {
            "time_utc": t,
            "area": area,
            "parameter": param_name,
            "station_id": station_id,
            "value": float(v),
            "quality": q if isinstance(q, str) else "",
            "inserted_at_utc": now,
        }
        for t, v, q in zip(ts, df["value"], df["quality"])
    ]


# ------------------------------------------------------------------- collect
def collect(mode: str = "latest", write: bool = False,
            since: str = HISTORY_START) -> pd.DataFrame:
    """Fetch all (area, parameter) combinations in the given mode."""
    handler = DataHandler(schema_mapper["weather_observations"], "smhi",
                          "weather_observations")
    all_rows: list[dict] = []
    for area in BIDDING_AREAS:
        for param_name, param_id in CFG["parameters"].items():
            station = get_station(area, param_name)
            try:
                if mode == "latest":
                    rows = fetch_latest_day(param_name, param_id, area, station)
                elif mode == "recent":
                    rows = fetch_archive(param_name, param_id, area, station,
                                         since, period="latest-months")
                else:
                    rows = fetch_archive(param_name, param_id, area, station, since)
            except Exception as e:  # noqa: BLE001
                logger.warning("skipping %s/%s (station %s): %s",
                               area, param_name, station, e)
                rows = []
            logger.info("smhi %s %s/%s station=%s -> %s rows",
                        mode, area, param_name, station, len(rows))
            all_rows.extend(rows)
            time.sleep(CFG["rate_limit_seconds"])
    if not all_rows:
        return pd.DataFrame()
    if write:
        label = {
            "latest": "latest_" + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "recent": f"recent_months_since_{since}",
        }.get(mode, f"archive_since_{since}")
        return handler.persist(all_rows, label)
    return handler.to_dataframe(all_rows)


# ------------------------------------------------------------------ validate
def validate_stations() -> list[str]:
    """Check every configured (area, parameter, station) combo exists on SMHI.

    Returns a list of problem descriptions (empty = all good).
    """
    problems = []
    for area in BIDDING_AREAS:
        for param_name, param_id in CFG["parameters"].items():
            station = get_station(area, param_name)
            url = (f"{CFG['base_url']}/parameter/{param_id}"
                   f"/station/{station}.json")
            try:
                meta = _get(url, max_retries=1).json()
                if not meta.get("active", False):
                    problems.append(
                        f"{area}/{param_name}: station {station} is INACTIVE")
            except Exception as e:  # noqa: BLE001
                problems.append(
                    f"{area}/{param_name}: station {station} unreachable ({e})")
            time.sleep(0.1)
    return problems
