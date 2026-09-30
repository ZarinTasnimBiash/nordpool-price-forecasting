"""Central configuration for all data ingestion (reference pattern: config over code).

Every API endpoint collected is described here — URL, parameters, storage target.
Adding a new source/endpoint should mostly mean adding config, not writing new logic.
Mirrors the reference platform's central-config + `api_config` convention.
"""

from pathlib import Path

# --- Project-wide constants -------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Swedish bidding areas, north to south (reference scope: SE1-SE4)
BIDDING_AREAS = ["SE1", "SE2", "SE3", "SE4"]

CURRENCY = "EUR"

# How far back the history backfill reaches (~3 years by default).
# Applies to elpriset (prices) and the SMHI corrected archive (weather).
HISTORY_START = "2023-09-01"

# The public Nord Pool Data Portal only serves a rolling ~62-day window; older
# dates return 401. Deeper history needs the paid/partner API,
# so Nord Pool is the *live daily* source only and elpriset supplies history.
#
# The window ROLLS: measured on 2026-08-19 the edge moved from T-63 to T-62
# within an hour. A fixed start_date therefore ages out of the window over
# time. This date is set well inside the window (not at its edge) so the daily
# job and the overlap check have margin; partitions older than the window will
# eventually 401 if materialized. See docs/02_data_access.md.
NORDPOOL_HISTORY_START = "2026-07-01"

# Archived weather FORECASTS (what was predicted, as of the day before) start
# later than the observations: day-1 forecasts appear from ~2024-01-22 and
# day-2 from ~2024-03. This date is where both are complete, so every forecast
# feature exists for every row. ~29 months, covering two full winters.
FORECAST_HISTORY_START = "2024-04-01"

# --- Storage (local substitute for GCS + BigQuery) --------------------------
# Reference platform: raw parquet in cloud object storage, tables in a cloud warehouse.
# Here:    raw parquet in data/raw/, tables in a local DuckDB file.

STORAGE = {
    "raw_dir": PROJECT_ROOT / "data" / "raw",
    "duckdb_path": PROJECT_ROOT / "data" / "warehouse.duckdb",
}

# --- API configuration ------------------------------------------------------

api_config = {
    # ------------------------------------------------------------------ Nord Pool
    # Public API behind https://data.nordpoolgroup.com (the Data Portal).
    # NOTE: requires browser-like headers, otherwise returns 401.
    "nordpool": {
        "base_url": "https://dataportal-api.nordpoolgroup.com/api",
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Origin": "https://data.nordpoolgroup.com",
            "Referer": "https://data.nordpoolgroup.com/",
        },
        # polite delay between calls when backfilling (seconds)
        "rate_limit_seconds": 0.3,
        "endpoints": {
            "day_ahead_prices": {
                "path": "DayAheadPrices",
                "params": {"market": "DayAhead"},
                "time_mode": "daily",
                "table_name": "nordpool_day_ahead_prices",
            },
            # NOTE (2026-08-19): a "day_ahead_volumes" endpoint used to be
            # configured here with path "DayAheadVolumes". Probing the live API
            # returns 404 — that path does not exist. It was written from a
            # guess while API access was unavailable, never from a real sample.
            # Volumes are out of scope until the real path/payload is captured
            # from the Data Portal's own network calls (browser F12 -> Network).
            # The schema class and normalizer are still in the repo, so
            # restoring this endpoint means adding config back here.
        },
    },
    # ----------------------------------------------------------------- elpriset
    # elprisetjustnu.se — free Swedish service republishing Nord Pool day-ahead
    # prices, with history back to 2021 and no account required. This is the
    # *historical* price source, because the Nord Pool public API only serves
    # ~63 days (see NORDPOOL_HISTORY_START). One request per (day, area).
    # URL shape: /api/v1/prices/<YYYY>/<MM-DD>_<AREA>.json
    "elpriset": {
        "base_url": "https://www.elprisetjustnu.se/api/v1/prices",
        "headers": {"User-Agent": "nordpool-price-forecasting/0.1 (internship study project)"},
        "rate_limit_seconds": 0.25,
        "endpoints": {
            "elpriset_day_ahead_prices": {
                "time_mode": "daily",
                "table_name": "elpriset_day_ahead_prices",
            },
        },
    },
    # --------------------------------------------------------------- openmeteo
    # Archived weather forecasts — what was FORECAST for a given hour, as issued
    # one or two days earlier. This is the only weather that is legitimately
    # available at the 11:00 cutoff for the delivery day itself; SMHI gives
    # what actually happened, which is only knowable afterwards.
    # `lead_days` 0 = the same provider's analysed value, kept so forecast error
    # can be measured within one source rather than across two.
    "openmeteo": {
        "base_url": "https://previous-runs-api.open-meteo.com/v1/forecast",
        "headers": {"User-Agent": "nordpool-price-forecasting/0.1 (internship study project)"},
        "rate_limit_seconds": 1.0,
        "table_name": "weather_forecast_openmeteo",
        # Open-Meteo variable -> local parameter name, matching the SMHI naming
        "variables": {
            "temperature_2m": "air_temperature",
            "wind_speed_10m": "wind_speed",
            "pressure_msl": "air_pressure_at_sea_level",
            "cloud_cover": "cloud_cover",
        },
        "lead_days": [1, 2],
        # Same coordinates as each area's default SMHI station, so the forecast
        # describes the same place the observation was taken.
        "points": {
            "SE1": {"lat": 65.543, "lon": 22.124},      # Lulea-Kallax
            "SE2": {"lat": 63.1974, "lon": 14.4863},    # Ostersund-Froson
            "SE3": {"lat": 59.341681, "lon": 18.054928},  # Stockholm-Observatoriekullen
            "SE4": {"lat": 55.6049, "lon": 12.9843},    # Malmo
        },
    },
    # ---------------------------------------------------------------------- SMHI
    # Swedish weather observations (reference platform's SMHI pipeline pattern:
    # one station mapped per bidding area, per-parameter overrides allowed).
    "smhi": {
        "base_url": "https://opendata-download-metobs.smhi.se/api/version/1.0",
        "endpoint": "weather_observations",
        "table_name": "weather_smhi_data",
        "time_mode": "daily",
        "rate_limit_seconds": 0.3,
        # SMHI parameter ids (the same six parameters the reference platform ingests)
        "parameters": {
            "air_temperature": 1,
            "wind_from_direction": 3,
            "wind_speed": 4,
            "relative_humidity": 6,
            "precipitation_amount_1h": 7,
            "air_pressure_at_sea_level": 9,
        },
        # Default station per bidding area (verified active via --validate,
        # Aug 2026):
        #   SE1 Luleå-Kallax Flygplats, SE2 Östersund-Frösön Flygplats,
        #   SE3 Stockholm-Observatoriekullen A, SE4 Malmö A
        "stations": {
            "SE1": 162860,
            "SE2": 134110,
            "SE3": 98230,
            "SE4": 52350,
        },
        # Per-parameter station overrides (reference: "each parameter has
        # area-specific station mapping"). Found via validation run:
        # - SE3: Stockholm-Observatoriekullen A no longer measures wind
        #        -> Stockholm-Bromma Flygplats (97200)
        # - SE2: Frösön airport has no 1h-precipitation gauge
        #        -> Föllinge A (134410), ~55 km north
        # - SE4: Malmö A has no pressure sensor
        #        -> Malmö-Sturup Flygplats (53300)
        "station_overrides": {
            "SE2": {
                "precipitation_amount_1h": 134410,
            },
            "SE3": {
                "wind_from_direction": 97200,
                "wind_speed": 97200,
            },
            "SE4": {
                "air_pressure_at_sea_level": 53300,
            },
        },
    },
}


def get_station(area: str, parameter_name: str) -> int:
    """Resolve the SMHI station for an (area, parameter) pair, honouring overrides."""
    cfg = api_config["smhi"]
    return cfg["station_overrides"].get(area, {}).get(
        parameter_name, cfg["stations"][area]
    )