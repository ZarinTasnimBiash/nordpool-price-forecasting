"""Schema class for elprisetjustnu.se day-ahead prices (historical source).

Deliberately a *separate table* from `nordpool_day_ahead_prices` rather than a
`source` column on one shared table:
- the two sources overlap for ~63 days, and keeping them apart allows a comparison
  them against each other as a data-quality check
- it follows the reference convention of one schema class per warehouse table

Column names mirror the Nord Pool price schema so the two are directly
comparable. `price_sek_kwh` and `exchange_rate` are kept because they come free
in the payload and explain any EUR discrepancy against Nord Pool (elpriset does
its own SEK->EUR conversion at a daily rate).
"""

from schemas.base_model import BaseSchema


class ElprisetDayAheadPrices(BaseSchema):
    """One row per (delivery period, bidding area)."""

    _table_name = "elpriset_day_ahead_prices"
    timestamp_field = "delivery_start_utc"
    columns = {
        "delivery_start_utc": "datetime64[ns, UTC]",
        "delivery_end_utc": "datetime64[ns, UTC]",
        "delivery_area": "string",
        "price_eur_mwh": "float64",
        "price_sek_kwh": "float64",
        "exchange_rate": "float64",
        "resolution_minutes": "int64",
        "delivery_date_cet": "string",
        "inserted_at_utc": "datetime64[ns, UTC]",
    }
