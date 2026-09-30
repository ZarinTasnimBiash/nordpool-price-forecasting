"""Stage 5 — the measuring apparatus, kept separate from the models themselves.

Three things live here, and they are the reason any number in Stage 5 can be
trusted:

WALK-FORWARD FOLDS
    A single train/test split is not enough. The six months held out during
    Stage 4 turned out to be a different price regime from the training period
    (median 57.9 against 31.2, negative hours 1.5% against 5.7%), so one score
    said as much about the window as about the model. Folds roll forward one
    month at a time, always training on the past and testing on the future.

    The first few folds are for tuning and the rest are never touched until the
    end — "tune on history, deploy forward", which is both simpler than nested
    cross-validation and harder to get subtly wrong.

TWO SCORINGS
    Per hour, because that is conventional and comparable to published work.
    Per forecast-day, because operationally one decision is made at 11:00
    covering 24 hours. The per-day *average* is nearly identical to the per-hour
    one — same errors, regrouped — so what matters there is the spread across
    days, not the centre.

THE NOISE RULE
    A change is only kept when its improvement survives a block bootstrap over
    whole days. Whole days because neighbouring hours correlate about 0.95;
    resampling hours independently would pretend there is far more evidence
    than there is and make everything look significant.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

LOCAL_TZ = "Europe/Stockholm"
MIN_TRAIN_MONTHS = 14
TEST_MONTHS_PER_FOLD = 1
TUNE_FOLDS = 5          # earliest folds; the rest stay untouched for reporting
N_BOOT = 2000

# Hour groups for the segmented model. Taken from the Stage 3 profile and the
# Stage 4 error-by-hour breakdown, not chosen arbitrarily: the trough sits
# around 03:00, the morning peak at 08:00, a midday dip at 13:00, and the
# evening peak at 19:00-20:00 where the single model is weakest.
HOUR_GROUPS = {
    "night":   [23, 0, 1, 2, 3, 4, 5],
    "morning": [6, 7, 8, 9, 10],
    "midday":  [11, 12, 13, 14, 15, 16],
    "evening": [17, 18, 19, 20, 21, 22],
}
GROUP_OF_HOUR = {h: g for g, hs in HOUR_GROUPS.items() for h in hs}


# ------------------------------------------------------------------- folds
def make_folds(index: pd.DatetimeIndex) -> list[dict]:
    """Expanding-window folds: train on everything before, test the next month."""
    local = index.tz_convert(LOCAL_TZ)
    month = pd.PeriodIndex(local.tz_localize(None), freq="M")
    months = sorted(month.unique())

    folds = []
    for i in range(MIN_TRAIN_MONTHS, len(months), TEST_MONTHS_PER_FOLD):
        test_months = months[i:i + TEST_MONTHS_PER_FOLD]
        if len(test_months) < TEST_MONTHS_PER_FOLD:
            break
        train_mask = month < test_months[0]
        test_mask = month.isin(test_months)
        folds.append({
            "label": str(test_months[0]),
            "train": train_mask,
            "test": test_mask,
            "purpose": "tune" if len(folds) < TUNE_FOLDS else "report",
        })
    return folds


def describe_folds(folds: list[dict], index: pd.DatetimeIndex) -> str:
    tune = [f for f in folds if f["purpose"] == "tune"]
    rep = [f for f in folds if f["purpose"] == "report"]
    return (f"{len(folds)} folds — {len(tune)} for tuning "
            f"({tune[0]['label']}..{tune[-1]['label']}), "
            f"{len(rep)} untouched for reporting "
            f"({rep[0]['label']}..{rep[-1]['label']})")


# ------------------------------------------------------------------ scoring
def per_hour(y_true, y_pred) -> dict:
    err = np.asarray(y_pred, float) - np.asarray(y_true, float)
    return {"MAE": float(np.abs(err).mean()),
            "RMSE": float(np.sqrt((err ** 2).mean()))}


def per_day(y_true, y_pred, index: pd.DatetimeIndex) -> dict:
    """Daily mean absolute error, summarised as a distribution over days.

    The mean of this is essentially the per-hour MAE — the same errors, grouped
    differently. The useful part is the spread: how bad is a bad day.
    """
    err = np.abs(np.asarray(y_pred, float) - np.asarray(y_true, float))
    day = index.tz_convert(LOCAL_TZ).normalize()
    daily = pd.Series(err, index=day).groupby(level=0).mean()
    return {"mean_day_MAE": float(daily.mean()),
            "median_day_MAE": float(daily.median()),
            "p90_day_MAE": float(daily.quantile(0.90)),
            "worst_day_MAE": float(daily.max()),
            "n_days": int(len(daily))}


# --------------------------------------------------------------- noise rule
def bootstrap_difference(y_true, pred_a, pred_b, index: pd.DatetimeIndex,
                         n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """Is A worse than B by more than noise? Positive means B is better.

    Resamples whole days, never individual hours.
    """
    y = np.asarray(y_true, float)
    a = np.abs(np.asarray(pred_a, float) - y)
    b = np.abs(np.asarray(pred_b, float) - y)

    day = index.tz_convert(LOCAL_TZ).normalize().to_numpy()
    uniq = np.unique(day)
    by_day = {d: np.where(day == d)[0] for d in uniq}
    rng = np.random.default_rng(seed)

    diffs = np.empty(n_boot)
    for i in range(n_boot):
        sel = np.concatenate([by_day[d] for d in rng.choice(uniq, len(uniq), True)])
        diffs[i] = a[sel].mean() - b[sel].mean()

    lo, hi = np.percentile(diffs, [2.5, 97.5])
    point = a.mean() - b.mean()
    return {"diff": float(point), "lo": float(lo), "hi": float(hi),
            "significant": bool(lo > 0 or hi < 0)}


def verdict(cmp: dict) -> str:
    if not cmp["significant"]:
        return "not distinguishable from noise"
    return "better" if cmp["diff"] > 0 else "worse"


# ------------------------------------------------------------- calibration
def interval_coverage(y_true, lower, upper) -> dict:
    """Does an interval that claims 80% actually contain 80% of outcomes?"""
    y = np.asarray(y_true, float)
    lo = np.asarray(lower, float)
    hi = np.asarray(upper, float)
    inside = (y >= lo) & (y <= hi)
    return {"coverage": float(inside.mean()),
            "mean_width": float((hi - lo).mean()),
            "below": float((y < lo).mean()),
            "above": float((y > hi).mean())}


def pinball_loss(y_true, y_pred, quantile: float) -> float:
    """The loss a quantile model is actually optimising; lower is better."""
    y = np.asarray(y_true, float)
    p = np.asarray(y_pred, float)
    delta = y - p
    return float(np.maximum(quantile * delta, (quantile - 1) * delta).mean())
