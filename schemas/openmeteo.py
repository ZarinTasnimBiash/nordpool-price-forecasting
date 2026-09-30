"""Schema class for archived weather forecasts (Open-Meteo previous runs).

Stored long, like the SMHI observations, but with one extra dimension that
carries the whole point of the table: `lead_days`.

    lead_days = 0   the provider's own analysed value for that hour
    lead_days = 1   what was forecast for that hour, one day earlier
    lead_days = 2   what was forecast for that hour, two days earlier

Only lead_days >= 1 is usable as a feature: those values existed before the
11:00 cutoff on the day the forecast is made. lead_days = 0 is kept so that
forecast error can be measured within a single provider, rather than by
comparing an Open-Meteo forecast against an SMHI station reading — which would
confound forecast error with the difference between a grid point and a mast.
"""

from schemas.base_model import BaseSchema


class WeatherForecast(BaseSchema):
    _table_name = "weather_forecast_openmeteo"
    timestamp_field = "time_utc"
    columns = {
        "time_utc": "datetime64[ns, UTC]",   # the hour the forecast is FOR
        "area": "string",
        "parameter": "string",
        "lead_days": "int64",                 # 0 = analysed, 1/2 = forecast age
        "value": "float64",
        "inserted_at_utc": "datetime64[ns, UTC]",
    }
