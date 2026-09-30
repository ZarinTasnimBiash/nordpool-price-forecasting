"""Schema classes for Nord Pool day-ahead market tables.

Note on resolution: the day-ahead market switched from 60-minute to 15-minute
periods in October 2025. I store rows at their native resolution and record it
in `resolution_minutes`; aggregation to a common hourly grain happens in the
feature-building stage (mirroring how the reference platform's aggregated view truncates
timestamps to the hour).
"""

from schemas.base_model import BaseSchema


class NordpoolDayAheadPrices(BaseSchema):
    """One row per (delivery period, bidding area): the auction price."""

    _table_name = "nordpool_day_ahead_prices"
    timestamp_field = "delivery_start_utc"
    columns = {
        "delivery_start_utc": "datetime64[ns, UTC]",
        "delivery_end_utc": "datetime64[ns, UTC]",
        "delivery_area": "string",
        "price_eur_mwh": "float64",
        "currency": "string",
        "resolution_minutes": "int64",
        "delivery_date_cet": "string",
        "updated_at_utc": "datetime64[ns, UTC]",
    }


class NordpoolDayAheadVolumes(BaseSchema):
    """One row per (delivery period, bidding area): traded buy/sell volume."""

    _table_name = "nordpool_day_ahead_volumes"
    timestamp_field = "delivery_start_utc"
    columns = {
        "delivery_start_utc": "datetime64[ns, UTC]",
        "delivery_end_utc": "datetime64[ns, UTC]",
        "delivery_area": "string",
        "volume_buy_mwh": "float64",
        "volume_sell_mwh": "float64",
        "resolution_minutes": "int64",
        "delivery_date_cet": "string",
        "updated_at_utc": "datetime64[ns, UTC]",
    }
