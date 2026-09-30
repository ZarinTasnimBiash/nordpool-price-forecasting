"""schema_mapper: endpoint name -> schema class (reference pattern).

This is the key contract between API payloads and warehouse tables. Both the
manual CLI path (ingestion/main.py) and the Dagster asset path resolve their
schema through this mapper, so they can never drift apart.
"""

from schemas.elpriset import ElprisetDayAheadPrices
from schemas.nordpool import NordpoolDayAheadPrices, NordpoolDayAheadVolumes
from schemas.openmeteo import WeatherForecast
from schemas.weather import WeatherObservation

schema_mapper = {
    "day_ahead_prices": NordpoolDayAheadPrices,
    "day_ahead_volumes": NordpoolDayAheadVolumes,
    "elpriset_day_ahead_prices": ElprisetDayAheadPrices,
    "weather_observations": WeatherObservation,
    "weather_forecast": WeatherForecast,
}
