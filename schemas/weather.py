"""Schema class for SMHI weather observations.

Design decision (documented deviation from the reference platform): it stores
weather in a *wide* table (one column per parameter). I store *long* format —
one row per (time, area, parameter) — because the per-parameter station
overrides mean different parameters for the same area can come from different
stations, which a wide row cannot represent honestly. The wide, model-ready
shape is produced later by the feature-building step (a pivot), matching the
reference platform's aggregated-view approach of shaping data in the warehouse layer.
"""

from schemas.base_model import BaseSchema


class WeatherObservation(BaseSchema):
    _table_name = "weather_smhi_data"
    timestamp_field = "time_utc"
    columns = {
        "time_utc": "datetime64[ns, UTC]",
        "area": "string",
        "parameter": "string",
        "station_id": "int64",
        "value": "float64",
        "quality": "string",  # SMHI quality code: G=green/checked, Y=yellow/suspect
        "inserted_at_utc": "datetime64[ns, UTC]",
    }
