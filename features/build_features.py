"""Stage 4 — build the model-ready feature table.

One row per delivery hour. The target is the SE3 day-ahead price for that hour;
every feature is something that was knowable at the moment the forecast is made.

THE TIMING RULE
---------------
The forecast for delivery day D+1 is made at 11:00 Europe/Stockholm on day D,
before the auction closes at 12:00. What is available at that moment differs by
source, and the difference matters:

* Prices  — the whole of day D is already published (it was released at ~13:00
  on day D-1), so a lag of 24h or more is safe for every target hour.
* Weather observations arrive in real time, so only data up to D 11:00 exists.
  A 24h lag on an OBSERVATION would leak: for target D+1 18:00 the reading at
  D 18:00 is still seven hours in the future. Observation features are therefore
  aggregates over a window ENDING AT THE CUTOFF.
* Weather FORECASTS are different, and this is the important point: a forecast
  for D+1 that was published before D 11:00 genuinely existed at the cutoff, so
  using it is not leakage. It is the only way to give the model information
  about the delivery day's own weather.

Measured: observation-only weather adds nothing (+0.07 MAE, CI spans zero),
while forecast weather is worth 3.74 EUR/MWh. See docs/04_features.md.

Run:  PYTHONPATH=. python features/build_features.py
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from config.configs import BIDDING_AREAS, FORECAST_HISTORY_START, STORAGE

TARGET_AREA = "SE3"
NEIGHBOURS = ["SE4", "SE1"]          # strongest and most independent correlates
PRICE_LAGS_H = [24, 48, 168]         # >= 24h: day D's prices are fully published
CUTOFF_HOUR_LOCAL = 11               # forecast is made at 11:00, auction closes 12:00
LOCAL_TZ = "Europe/Stockholm"
TABLE = "features_se3_hourly"

# Expand each delivery period onto the UTC hours it covers. Where an hour is
# covered twice — which happens on the three DST fall-back days — the shorter,
# more specific period wins. Quarter-hourly periods are averaged into the hour.
PRICE_GRID_SQL = """
with expanded as (
  select delivery_area as area,
         unnest(generate_series(date_trunc('hour', delivery_start_utc),
                                delivery_end_utc - interval '1 microsecond',
                                interval '1 hour')) as hour_utc,
         price_eur_mwh, resolution_minutes
  from elpriset_day_ahead_prices
),
ranked as (
  select *, min(resolution_minutes) over (partition by hour_utc, area) as best
  from expanded
)
select hour_utc, area, avg(price_eur_mwh) as price
from ranked
where resolution_minutes = best
group by 1, 2
"""

WEATHER_SQL = """
select date_trunc('hour', time_utc) as hour_utc, area, parameter, avg(value) as value
from weather_smhi_data
group by 1, 2, 3
"""

# Archived forecasts: what each hour was PREDICTED to be, one or two days
# earlier. lead_days 0 (the provider's analysis) is excluded — it is only kept
# in the warehouse so forecast error can be measured, and it is not knowable at
# the cutoff.
FORECAST_SQL = """
select time_utc as hour_utc, area, parameter, lead_days, value
from weather_forecast_openmeteo
"""


# --------------------------------------------------------------------- load
def load_grids() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Hourly price matrix, observed-weather matrix, and forecast matrix."""
    con = duckdb.connect(str(STORAGE["duckdb_path"]), read_only=True)
    con.execute("set enable_progress_bar=false")
    con.execute("set TimeZone='UTC'")
    # Single-threaded on purpose. avg() is a parallel aggregation and its
    # floating-point summation order is not fixed between runs, so the same
    # query returns values differing in their last bits. Those differences move
    # gradient-boosting bin edges, change tree splits, and shifted model MAE by
    # ~0.08 EUR/MWh run to run — the same size as the effects being measured.
    # Totals are unaffected; only reproducibility is. These queries are small.
    con.execute("set threads=1")
    px = con.execute(PRICE_GRID_SQL).df()
    wx = con.execute(WEATHER_SQL).df()
    try:
        fx = con.execute(FORECAST_SQL).df()
    except Exception:          # table absent until the forecast backfill runs
        fx = pd.DataFrame(columns=["hour_utc", "area", "parameter", "lead_days", "value"])
    con.close()

    px["hour_utc"] = pd.to_datetime(px["hour_utc"], utc=True)
    prices = px.pivot(index="hour_utc", columns="area", values="price").sort_index()

    wx["hour_utc"] = pd.to_datetime(wx["hour_utc"], utc=True)
    weather = wx.pivot_table(index="hour_utc", columns=["area", "parameter"],
                             values="value").sort_index()
    weather.columns = [f"{p}_{a.lower()}" for a, p in weather.columns]

    # a complete hourly index is what makes .shift() a true time lag
    full = pd.date_range(prices.index.min(), prices.index.max(), freq="h", tz="UTC")
    assert prices.index.equals(full), "price grid has gaps — lags would be wrong"

    if len(fx):
        fx["hour_utc"] = pd.to_datetime(fx["hour_utc"], utc=True)
        forecast = fx.pivot_table(index="hour_utc",
                                  columns=["area", "parameter", "lead_days"],
                                  values="value")
        forecast.columns = [f"{p}_{a.lower()}_fc{int(l)}" for a, p, l in forecast.columns]
        forecast = forecast.sort_index().reindex(full)
    else:
        forecast = pd.DataFrame(index=full)

    return prices, weather.reindex(full), forecast


# ------------------------------------------------------------------ cutoff
def cutoff_for(target_hours: pd.DatetimeIndex) -> pd.Series:
    """The instant the forecast is made, for each target delivery hour.

    Delivery day is read in local time; the cutoff is 11:00 local on the day
    before, converted back to UTC.
    """
    # Do the day arithmetic on NAIVE local wall-clock time. Subtracting
    # Timedelta(days=1) from a tz-aware stamp removes 24 absolute hours, which
    # on a 25-hour DST day lands at 01:00 rather than midnight and shifts the
    # cutoff to 12:00. Naive arithmetic is calendar arithmetic, which is what
    # "11:00 the day before" actually means.
    local_naive = target_hours.tz_convert(LOCAL_TZ).tz_localize(None)
    prev_day = local_naive.normalize() - pd.Timedelta(days=1)
    cutoff_local = prev_day + pd.Timedelta(hours=CUTOFF_HOUR_LOCAL)
    # DST-safe: localise the naive local stamp, then convert to UTC
    cutoff = (pd.DatetimeIndex(cutoff_local)
              .tz_localize(LOCAL_TZ, ambiguous=True, nonexistent="shift_forward")
              .tz_convert("UTC"))
    return pd.Series(cutoff, index=target_hours, name="cutoff_utc")


# ---------------------------------------------------------------- features
def weather_features(weather: pd.DataFrame, idx: pd.DatetimeIndex,
                     cutoff: pd.Series, mode: str,
                     forecast: pd.DataFrame | None = None) -> pd.DataFrame:
    """Weather columns under one of three availability assumptions.

    cutoff      — 24h aggregates read AT the 11:00 cutoff. Honest, but the same
                  value for all 24 target hours: no hour-to-hour shape.
    persistence — the observation at the SAME CLOCK HOUR inside the last full
                  24h before the cutoff. Also honest (that window is entirely
                  observed), and it restores the daily shape. This is the
                  zero-skill forecast: "tomorrow will be like the last day".
    perfect     — the observation AT the target hour. LEAKS: unknowable at the
                  cutoff. Used only to measure the ceiling that perfect weather
                  knowledge would buy.
    """
    if mode == "none":
        # no weather columns at all — the control, to show whether weather
        # earns its place given that tomorrow's weather is never known
        return pd.DataFrame(index=idx)

    if mode == "perfect":
        return weather.reindex(idx).add_suffix("_at_hour")

    if mode == "persistence":
        # the 24h window ending at the cutoff contains each clock hour once;
        # step back whole days from the target until landing inside it
        src = idx.copy()
        cut = pd.DatetimeIndex(cutoff)
        while True:
            late = src >= cut
            if not late.any():
                break
            src = src.where(~late, src - pd.Timedelta(days=1))
            src = pd.DatetimeIndex(src)
        assert (src < cut).all(), "persistence source is not before the cutoff"
        assert (src >= cut - pd.Timedelta(hours=24)).all(), "persistence source too old"
        out = weather.reindex(src)
        out.index = idx
        return out.add_suffix("_persist")

    if mode == "perfect_om":
        # The SAME provider's analysed value at the target hour. Leaks, exactly
        # like "perfect", but built from the identical source and variable set
        # as the forecast modes — so forecast-vs-this measures forecast skill
        # alone, with no grid-versus-station confound.
        cols = [c for c in (forecast.columns if forecast is not None else []) if c.endswith("_fc0")]
        assert cols, "perfect_om needs lead-0 columns"
        out = forecast[cols].reindex(idx)
        out.index = idx
        return out

    if mode not in ("cutoff", "forecast", "forecast_d2"):
        raise ValueError(f"unknown weather mode: {mode}")

    wanted = {"air_temperature": ["mean", "min", "max"],
              "wind_speed": ["mean", "max"],
              "air_pressure_at_sea_level": ["mean"]}
    frames = {}
    for param, stats in wanted.items():
        for area in BIDDING_AREAS:
            col = f"{param}_{area.lower()}"
            if col not in weather.columns:
                continue
            roll = weather[col].rolling("24h", closed="right")
            for stat in stats:
                frames[f"{col}_24h_{stat}"] = getattr(roll, stat)()
    agg = pd.DataFrame(frames)

    # NOTE: pd.DatetimeIndex(cutoff.values) would silently drop the timezone and
    # match nothing, filling every weather column with nulls. Build from the
    # Series so the tz survives.
    cutoff_idx = pd.DatetimeIndex(cutoff)
    assert cutoff_idx.tz is not None, "cutoff index lost its timezone"
    out = agg.reindex(cutoff_idx)
    out.index = idx

    if mode in ("forecast", "forecast_d2"):
        # What the target hour was FORECAST to be, as issued a day (or two)
        # earlier. This is genuinely available at the cutoff, so it is not
        # leakage — unlike the observation for the same hour.
        assert forecast is not None and len(forecast.columns), \
            "forecast mode needs the forecast grid (run the openmeteo backfill)"
        lead = 1 if mode == "forecast" else 2
        cols = [c for c in forecast.columns if c.endswith(f"_fc{lead}")]
        assert cols, f"no forecast columns at lead {lead}"
        fc = forecast[cols].reindex(idx)
        out = pd.concat([out, fc], axis=1)

    return out


def build(prices: pd.DataFrame, weather: pd.DataFrame,
          weather_mode: str = "forecast_d2",
          forecast: pd.DataFrame | None = None) -> pd.DataFrame:
    idx = prices.index
    f = pd.DataFrame(index=idx)
    f.index.name = "hour_utc"

    f["target_spot_price_se3"] = prices[TARGET_AREA]
    cutoff = cutoff_for(idx)
    f["cutoff_utc"] = cutoff

    # --- price lags (>= 24h, so day D is fully published) ------------------
    for area in [TARGET_AREA, *NEIGHBOURS]:
        for lag in PRICE_LAGS_H:
            f[f"spot_price_{area.lower()}_lag_{lag}h"] = prices[area].shift(lag)

    # --- rolling price stats over the 24h ending 24h before the target -----
    for area in [TARGET_AREA, *NEIGHBOURS]:
        base = prices[area].shift(24)
        f[f"spot_price_{area.lower()}_roll_24h_mean"] = base.rolling(24).mean()
        f[f"spot_price_{area.lower()}_roll_24h_min"] = base.rolling(24).min()
        f[f"spot_price_{area.lower()}_roll_24h_max"] = base.rolling(24).max()
    f["spot_price_se3_roll_168h_mean"] = prices[TARGET_AREA].shift(24).rolling(168).mean()

    # the north-south spread carries congestion information
    f["spread_se4_se1_lag_24h"] = (prices["SE4"] - prices["SE1"]).shift(24)

    # --- calendar (always knowable in advance) -----------------------------
    local = idx.tz_convert(LOCAL_TZ)
    f["hour_of_day"] = local.hour
    f["day_of_week"] = local.dayofweek
    f["month"] = local.month
    f["is_weekend"] = (local.dayofweek >= 5).astype("int64")
    # cyclical, because hour 23 is adjacent to hour 0
    for name, values, period in [("hour", local.hour, 24),
                                 ("dow", local.dayofweek, 7),
                                 ("month", local.month - 1, 12)]:
        f[f"{name}_sin"] = np.sin(2 * np.pi * np.asarray(values) / period)
        f[f"{name}_cos"] = np.cos(2 * np.pi * np.asarray(values) / period)

    # --- weather, under the chosen availability assumption -----------------
    wf = weather_features(weather, idx, cutoff, weather_mode, forecast)
    for col in wf.columns:
        f[col] = wf[col]

    if len(wf.columns):
        filled = f[list(wf.columns)].notna().any()
        assert filled.all(), f"weather features are entirely null: {list(filled[~filled].index)}"

    return f


def drop_dst_unsafe(f: pd.DataFrame) -> pd.DataFrame:
    """Remove target hours whose 24h lag would land inside their own delivery day.

    On the three DST fall-back days the local day has 25 hours, so subtracting
    24 UTC hours from its last hour lands on its *first* hour — a price from the
    same auction, which is not published at the 11:00 cutoff. A fixed 24h lag
    cannot escape a 25-hour day, so those rows are dropped rather than fudged.
    """
    day_start = f.index.tz_convert(LOCAL_TZ).normalize().tz_convert("UTC")
    unsafe = (f.index - pd.Timedelta(hours=min(PRICE_LAGS_H))) >= day_start
    if unsafe.any():
        days = sorted({t.tz_convert(LOCAL_TZ).date().isoformat() for t in f.index[unsafe]})
        print(f"  dropped {unsafe.sum()} DST-unsafe target hour(s) on {', '.join(days)}")
    return f[~unsafe]


def finalise(f: pd.DataFrame) -> pd.DataFrame:
    """Drop the warm-up rows that cannot have full history."""
    feature_cols = [c for c in f.columns if c not in ("target_spot_price_se3", "cutoff_utc")]
    before = len(f)
    out = f.dropna(subset=["target_spot_price_se3"])
    out = out.dropna(subset=[c for c in feature_cols if c.startswith("spot_price")])
    print(f"  dropped {before - len(out):,} warm-up/incomplete rows -> {len(out):,} usable")
    return drop_dst_unsafe(out)


# ------------------------------------------------------------------- audit
def audit_leakage(f: pd.DataFrame, prices: pd.DataFrame) -> None:
    """Every price lag must resolve to a timestamp at or before the cutoff...

    ...except that day D's prices are published before the cutoff, so the true
    test for a price lag is `lagged timestamp < start of delivery day D+1`.
    Weather aggregates are checked against the cutoff directly.
    """
    idx = f.index
    delivery_day_start = (idx.tz_convert(LOCAL_TZ).normalize().tz_convert("UTC"))

    for lag in PRICE_LAGS_H:
        lagged = idx - pd.Timedelta(hours=lag)
        bad = (lagged >= delivery_day_start).sum()
        assert bad == 0, f"price lag {lag}h reaches into the delivery day ({bad} rows)"

    # weather is read at the cutoff, so it can never be later than the cutoff
    assert (f["cutoff_utc"] < idx).all(), "cutoff is not strictly before the target hour"

    gap = (idx - pd.DatetimeIndex(f["cutoff_utc"])).total_seconds() / 3600
    assert gap.min() >= 13 - 1e-9, f"gap smaller than expected: {gap.min()}h"
    print(f"  leakage audit passed — cutoff-to-target gap {gap.min():.0f}h to {gap.max():.0f}h")


# -------------------------------------------------------------------- main
def main() -> None:
    print("loading grids ...")
    prices, weather, forecast = load_grids()
    print(f"  prices  {prices.shape[0]:,} hours x {prices.shape[1]} areas")
    print(f"  weather {weather.shape[0]:,} hours x {weather.shape[1]} columns")
    print(f"  forecast {forecast.shape[0]:,} hours x {forecast.shape[1]} columns")

    print("building features ...")
    # forecast_d2, not forecast_d1, on purpose: the two score identically
    # (difference -0.22, 95% CI [-0.81, +0.37]) but a forecast issued two days
    # before the delivery day is unambiguously older than the 11:00 cutoff,
    # whereas a one-day-old forecast depends on which model run it came from.
    # Zero measured cost, no residual leakage question.
    f = build(prices, weather, weather_mode="forecast_d2", forecast=forecast)
    f = f[f.index >= pd.Timestamp(FORECAST_HISTORY_START, tz="UTC")]
    f = finalise(f)
    audit_leakage(f, prices)

    out = f.reset_index()
    con = duckdb.connect(str(STORAGE["duckdb_path"]))
    con.execute("set enable_progress_bar=false")
    con.register("incoming", out)
    con.execute(f"create or replace table {TABLE} as select * from incoming")
    n = con.execute(f"select count(*) from {TABLE}").fetchone()[0]
    con.close()

    path = STORAGE["raw_dir"].parent / "processed" / f"{TABLE}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)

    print(f"wrote {n:,} rows x {len(out.columns)} columns -> {TABLE} and {path}")
    print(f"  span {out['hour_utc'].min()} .. {out['hour_utc'].max()}")


if __name__ == "__main__":
    main()
