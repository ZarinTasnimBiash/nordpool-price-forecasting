"""Step 0 — how much is weather knowledge actually worth?

Before building any forecast-ingestion machinery, measure whether it would pay.
The same model is trained three times, changing only what it is allowed to know
about the weather:

  cutoff      honest, what ships today: 24h aggregates read at the 11:00
              cutoff, so every hour of a delivery day sees the same weather
  persistence honest, and shaped: the observation at the same clock hour inside
              the last fully-observed 24h. The zero-skill forecast.
  perfect     CHEATING: the observation at the target hour itself. Impossible in
              production; it measures the ceiling that a perfect forecast buys.

The gap between `cutoff` and `perfect` is the most that better weather data
could ever be worth. If it is small, the forecast pipeline is not worth building.

Run:  PYTHONPATH=. python models/weather_value_experiment.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.configs import FORECAST_HISTORY_START
from features.build_features import build, drop_dst_unsafe, load_grids

TEST_MONTHS = 6
SEED = 0
N_BOOT = 2000

# Archived forecasts only exist from FORECAST_HISTORY_START. Every mode is
# restricted to that window so all of them see exactly the same rows — comparing
# a forecast model on less data against an observation model on more would
# measure the sample size, not the features.
MODES = ["none", "cutoff", "persistence", "perfect", "perfect_om",
         "forecast", "forecast_d2"]


def assemble(prices, weather, mode: str, forecast=None) -> pd.DataFrame:
    f = build(prices, weather, weather_mode=mode, forecast=forecast)
    f = f[f.index >= pd.Timestamp(FORECAST_HISTORY_START, tz="UTC")]
    f = f.dropna(subset=["target_spot_price_se3"])
    price_cols = [c for c in f.columns if c.startswith("spot_price")]
    f = f.dropna(subset=price_cols)
    return drop_dst_unsafe(f)


def split(f: pd.DataFrame):
    """Chronological split — never random: adjacent hours are 0.95 correlated."""
    cut = f.index.max() - pd.DateOffset(months=TEST_MONTHS)
    return f[f.index <= cut], f[f.index > cut]


def scores(y_true, y_pred) -> dict:
    err = np.asarray(y_pred) - np.asarray(y_true)
    return {"MAE": float(np.abs(err).mean()),
            "RMSE": float(np.sqrt((err ** 2).mean()))}


def fit_predict(train, test, seed: int) -> np.ndarray:
    """Histogram gradient boosting.

    scikit-learn's implementation rather than LightGBM: same family of
    algorithm, already a dependency, handles NaN natively, and needs no
    system-level libomp (which LightGBM does, and which is not installed here).
    """
    from sklearn.ensemble import HistGradientBoostingRegressor

    drop = ["target_spot_price_se3", "cutoff_utc"]
    xcols = [c for c in train.columns if c not in drop]
    # early_stopping defaults to on above 10k rows and carves out a RANDOM
    # validation slice — non-deterministic, and wrong for a time series, where
    # a random slice leaks near-duplicate neighbouring hours. Turned off so the
    # comparison between weather modes is exact and repeatable.
    model = HistGradientBoostingRegressor(
        max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
        min_samples_leaf=40, l2_regularization=1.0,
        early_stopping=False, random_state=seed,
    )
    model.fit(train[xcols], train["target_spot_price_se3"])
    return model.predict(test[xcols])


def main() -> None:
    prices, weather, forecast = load_grids()

    # The gaps between weather modes are small, so the uncertainty that matters
    # is sampling error on the test set, not the model seed (the fit is exactly
    # reproducible — see the threads=1 note in build_features.load_grids).
    # Differences are therefore reported with a block bootstrap over whole days,
    # because neighbouring hours are ~0.95 correlated and resampling individual
    # hours would understate the uncertainty badly.
    results, tables, preds = {}, {}, {}
    for mode in MODES:
        f = assemble(prices, weather, mode, forecast)
        tr, te = split(f)
        tables[mode] = (tr, te)
        preds[mode] = fit_predict(tr, te, SEED)
        results[mode] = scores(te["target_spot_price_se3"], preds[mode])

    # baselines use no weather at all, so one table is enough
    tr, te = tables["cutoff"]
    y = te["target_spot_price_se3"]
    baselines = {
        "baseline: yesterday same hour": scores(y, te["spot_price_se3_lag_24h"]),
        "baseline: last week same hour": scores(y, te["spot_price_se3_lag_168h"]),
        "baseline: train mean": scores(y, np.full(len(y), tr["target_spot_price_se3"].mean())),
    }

    print(f"\ntrain {len(tr):,} rows  ->  {tr.index.min().date()} .. {tr.index.max().date()}")
    print(f"test  {len(te):,} rows  ->  {te.index.min().date()} .. {te.index.max().date()}")
    print(f"\n  {'':38s} {'MAE':>7s} {'RMSE':>8s}")
    for name, sc in baselines.items():
        print(f"  {name:38s} {sc['MAE']:7.2f} {sc['RMSE']:8.2f}")
    print()
    for mode in MODES:
        label = {"none": "model, NO weather at all",
                 "cutoff": "model + weather @ cutoff (honest)",
                 "persistence": "model + persistence weather (honest)",
                 "perfect": "model + PERFECT weather, SMHI (leaks)",
                 "perfect_om": "model + PERFECT weather, same src (leaks)",
                 "forecast": "model + FORECAST weather (d-1, honest)",
                 "forecast_d2": "model + FORECAST weather (d-2, honest)"}[mode]
        sc = results[mode]
        print(f"  {label:38s} {sc['MAE']:7.2f} {sc['RMSE']:8.2f}")

    # ---- block bootstrap over days -------------------------------------
    te = tables["cutoff"][1]
    y = te["target_spot_price_se3"].to_numpy()
    days = te.index.tz_convert("Europe/Stockholm").normalize().to_numpy()
    uniq = np.unique(days)
    by_day = {d: np.where(days == d)[0] for d in uniq}
    rng = np.random.default_rng(SEED)
    picks = [np.concatenate([by_day[d] for d in rng.choice(uniq, len(uniq), True)])
             for _ in range(N_BOOT)]

    def compare(a, b, label):
        point = np.abs(preds[a] - y).mean() - np.abs(preds[b] - y).mean()
        d = np.array([np.abs(preds[a][s] - y[s]).mean() - np.abs(preds[b][s] - y[s]).mean()
                      for s in picks])
        lo, hi = np.percentile(d, [2.5, 97.5])
        sig = "REAL" if lo > 0 or hi < 0 else "not distinguishable from zero"
        print(f"  {label:40s} {point:+6.2f}  95% CI [{lo:+.2f}, {hi:+.2f}]  {sig}")

    print(f"\n  differences, with a block bootstrap over {len(uniq)} test days "
          f"({N_BOOT} resamples):")
    compare("none", "cutoff", "observed weather vs no weather")
    compare("cutoff", "forecast", "forecast (d-1) vs observed-only")
    compare("cutoff", "forecast_d2", "forecast (d-2) vs observed-only")
    compare("forecast", "perfect_om", "same-source perfect vs forecast (d-1)")
    compare("perfect", "perfect_om", "same-source perfect vs SMHI perfect")
    compare("forecast_d2", "forecast", "forecast d-1 vs d-2")
    compare("none", "forecast", "forecast (d-1) vs no weather")
    best = min(baselines, key=lambda k: baselines[k]["MAE"])
    print(f"\n  model beats the best baseline ({best.split(': ')[1]}) by "
          f"{baselines[best]['MAE'] - results['cutoff']['MAE']:.2f} EUR/MWh")


if __name__ == "__main__":
    main()
