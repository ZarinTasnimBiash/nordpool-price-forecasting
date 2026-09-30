"""Offline tests for the stage-4 feature builder.

Three of these are regression tests for bugs the leakage audit caught while the
builder was being written; each would silently corrupt training data:

1. the cutoff drifted to 12:00 on 25-hour DST days, because Timedelta(days=1)
   subtracts 24 absolute hours rather than one calendar day
2. a fixed 24h price lag lands inside the delivery day on those same days
3. the weather lookup lost its timezone and matched nothing, filling every
   weather column with nulls without raising
"""

import duckdb
import numpy as np
import pandas as pd
import pytest

from features.build_features import (LOCAL_TZ, PRICE_GRID_SQL, build,
                                     cutoff_for, drop_dst_unsafe)


def hours(start: str, periods: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=periods, freq="h", tz="UTC")


# ------------------------------------------------------------------ cutoff
def test_cutoff_is_11_local_on_ordinary_days():
    # start at local midnight, so all 24 hours belong to one local delivery day
    idx = hours("2025-01-14 23:00", 24)
    cut = cutoff_for(idx).dt.tz_convert(LOCAL_TZ)
    assert (cut.dt.hour == 11).all()
    assert (cut.dt.date == pd.Timestamp("2025-01-14").date()).all()


def test_cutoff_stays_11_local_across_the_25_hour_day():
    """Regression: Timedelta(days=1) is 24 absolute hours, not a calendar day."""
    # 2025-10-26 is the fall-back Sunday; the day after is the first affected one
    idx = hours("2025-10-26 23:00", 24)
    cut = cutoff_for(idx).dt.tz_convert(LOCAL_TZ)
    assert (cut.dt.hour == 11).all(), f"cutoff drifted to {sorted(set(cut.dt.hour))}"


def test_cutoff_gap_is_never_less_than_13_hours():
    idx = hours("2023-09-01 00:00", 24 * 400)
    gap = (idx - pd.DatetimeIndex(cutoff_for(idx))).total_seconds() / 3600
    assert gap.min() >= 13
    # 37h occurs once a year: the last hour of the 25-hour fall-back day, which
    # build_features drops because its 24h lag would land inside the same day
    assert gap.max() <= 37


# ------------------------------------------------------- DST-unsafe target
def test_drops_target_hours_whose_lag_lands_in_their_own_delivery_day():
    """Regression: a 24h lag cannot escape a 25-hour delivery day."""
    idx = hours("2023-10-28 22:00", 60)
    f = pd.DataFrame({"x": range(len(idx))}, index=idx)
    kept = drop_dst_unsafe(f)
    dropped = idx.difference(kept.index)
    assert len(dropped) == 1
    # it is the final hour of the 25-hour local day
    assert dropped[0].tz_convert(LOCAL_TZ).strftime("%Y-%m-%d %H") == "2023-10-29 23"


# -------------------------------------------------------------- price grid
def test_price_grid_prefers_the_shorter_overlapping_period():
    """The DST fall-back overlap: a 2h period and a 1h period cover one hour."""
    con = duckdb.connect()
    con.execute("set TimeZone='UTC'")
    con.execute("""
        create table elpriset_day_ahead_prices as select * from (values
          (timestamptz '2023-10-28 22:00:00+00', timestamptz '2023-10-28 23:00:00+00', 'SE3', 34.33, 60),
          (timestamptz '2023-10-28 23:00:00+00', timestamptz '2023-10-29 00:00:00+00', 'SE3', 27.19, 60),
          -- the 2-hour period spanning the repeated local hour
          (timestamptz '2023-10-29 00:00:00+00', timestamptz '2023-10-29 02:00:00+00', 'SE3', 25.34, 120),
          -- the 1-hour period covering the second half of it
          (timestamptz '2023-10-29 01:00:00+00', timestamptz '2023-10-29 02:00:00+00', 'SE3', 22.35, 60)
        ) t(delivery_start_utc, delivery_end_utc, delivery_area, price_eur_mwh, resolution_minutes)
    """)
    grid = con.execute(PRICE_GRID_SQL).df().set_index("hour_utc").sort_index()
    con.close()

    got = {str(pd.Timestamp(k).tz_localize("UTC") if pd.Timestamp(k).tz is None
               else pd.Timestamp(k)): round(v, 2)
           for k, v in grid["price"].items()}
    # the hour covered only by the long period keeps its price
    assert got["2023-10-29 00:00:00+00:00"] == 25.34
    # the contested hour takes the shorter, more specific period
    assert got["2023-10-29 01:00:00+00:00"] == 22.35
    assert len(grid) == 4


def test_price_grid_averages_quarter_hours_into_the_hour():
    con = duckdb.connect()
    con.execute("set TimeZone='UTC'")
    con.execute("""
        create table elpriset_day_ahead_prices as select * from (values
          (timestamptz '2026-01-01 00:00:00+00', timestamptz '2026-01-01 00:15:00+00', 'SE3', 10.0, 15),
          (timestamptz '2026-01-01 00:15:00+00', timestamptz '2026-01-01 00:30:00+00', 'SE3', 20.0, 15),
          (timestamptz '2026-01-01 00:30:00+00', timestamptz '2026-01-01 00:45:00+00', 'SE3', 30.0, 15),
          (timestamptz '2026-01-01 00:45:00+00', timestamptz '2026-01-01 01:00:00+00', 'SE3', 40.0, 15)
        ) t(delivery_start_utc, delivery_end_utc, delivery_area, price_eur_mwh, resolution_minutes)
    """)
    grid = con.execute(PRICE_GRID_SQL).df()
    con.close()
    assert len(grid) == 1
    assert grid["price"].iloc[0] == pytest.approx(25.0)  # mean of 10/20/30/40


# ----------------------------------------------------------------- lagging
AREAS = ["SE1", "SE2", "SE3", "SE4"]
PARAMS = ["air_temperature", "wind_speed", "air_pressure_at_sea_level"]


def _synthetic_grids(n_hours: int = 24 * 30):
    idx = hours("2025-01-01 00:00", n_hours)
    prices = pd.DataFrame({a: np.arange(n_hours, dtype=float) + i * 1000
                           for i, a in enumerate(AREAS)}, index=idx)
    weather = pd.DataFrame(
        {f"{p}_{a.lower()}": np.linspace(0, 10, n_hours) for a in AREAS for p in PARAMS},
        index=idx)
    # forecasts get a distinct offset per lead time so a test can prove which
    # one a feature actually came from
    forecast = pd.DataFrame(
        {f"{p}_{a.lower()}_fc{lead}": np.linspace(0, 10, n_hours) + 100 * lead
         for a in AREAS for p in PARAMS for lead in (0, 1, 2)},
        index=idx)
    return prices, weather, forecast


def test_lag_columns_are_true_time_lags():
    prices, weather, forecast = _synthetic_grids()
    f = build(prices, weather, forecast=forecast)
    row = f.index[500]
    assert f.loc[row, "spot_price_se3_lag_24h"] == prices["SE3"].loc[row - pd.Timedelta(hours=24)]
    assert f.loc[row, "spot_price_se4_lag_168h"] == prices["SE4"].loc[row - pd.Timedelta(hours=168)]


def test_weather_features_are_not_silently_all_null():
    """Regression: dropping the tz from the cutoff index matched no rows."""
    prices, weather, forecast = _synthetic_grids()
    f = build(prices, weather, forecast=forecast)   # build() asserts this internally
    assert f["air_temperature_se3_24h_mean"].notna().any()


def test_calendar_features_are_cyclical():
    prices, weather, forecast = _synthetic_grids()
    f = build(prices, weather, forecast=forecast)
    # hour 23 and hour 0 must sit next to each other on the circle
    h0 = f[f["hour_of_day"] == 0].iloc[0]
    h23 = f[f["hour_of_day"] == 23].iloc[0]
    dist = np.hypot(h0["hour_sin"] - h23["hour_sin"], h0["hour_cos"] - h23["hour_cos"])
    assert dist == pytest.approx(2 * np.sin(np.pi / 24), abs=1e-9)


# ---------------------------------------------------------------- forecasts
def test_forecast_features_read_the_target_hour_at_the_right_lead():
    """The forecast feature must be the forecast FOR this hour, at the lead asked for."""
    prices, weather, forecast = _synthetic_grids()
    row = hours("2025-01-01 00:00", 24 * 30)[400]

    f1 = build(prices, weather, weather_mode="forecast", forecast=forecast)
    f2 = build(prices, weather, weather_mode="forecast_d2", forecast=forecast)

    # lead 1 columns carry the +100 offset, lead 2 the +200 offset
    assert f1.loc[row, "air_temperature_se3_fc1"] == forecast.loc[row, "air_temperature_se3_fc1"]
    assert f2.loc[row, "air_temperature_se3_fc2"] == forecast.loc[row, "air_temperature_se3_fc2"]
    # and each mode exposes only its own lead
    assert "air_temperature_se3_fc2" not in f1.columns
    assert "air_temperature_se3_fc1" not in f2.columns


def test_forecast_modes_never_expose_the_analysed_value():
    """lead_days = 0 is the actual outcome — it must never reach a feature."""
    prices, weather, forecast = _synthetic_grids()
    for mode in ("forecast", "forecast_d2", "cutoff"):
        f = build(prices, weather, weather_mode=mode, forecast=forecast)
        leaked = [c for c in f.columns if c.endswith("_fc0")]
        assert not leaked, f"{mode} exposed analysed values: {leaked}"


def test_forecast_mode_still_includes_the_observed_aggregates():
    """Forecasts are added to the observed features, not swapped for them."""
    prices, weather, forecast = _synthetic_grids()
    f = build(prices, weather, weather_mode="forecast_d2", forecast=forecast)
    assert "air_temperature_se3_24h_mean" in f.columns     # observed, at the cutoff
    assert "air_temperature_se3_fc2" in f.columns          # forecast, for the hour


def test_forecast_mode_requires_a_forecast_grid():
    prices, weather, _ = _synthetic_grids()
    with pytest.raises(AssertionError, match="forecast"):
        build(prices, weather, weather_mode="forecast_d2", forecast=None)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
