"""Offline tests for the Stage 5 evaluation machinery.

These guard the parts that would silently invalidate every model number:
folds that leak the future into training, early stopping that peeks at the
validation set the wrong way round, and a bootstrap that overstates evidence.
"""

import numpy as np
import pandas as pd
import pytest

from models import evaluation as ev
from models.predictors import (ConformalQuantileModel, LinearModel,
                               _validation_score)


def hourly(start: str, n: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=n, freq="h", tz="UTC")


# --------------------------------------------------------------------- folds
def _folds(months: int = 24):
    idx = hourly("2024-04-01", 24 * 30 * months)
    return idx, ev.make_folds(idx)


def test_every_fold_trains_only_on_the_past():
    """The whole point of walk-forward: no training row may follow a test row."""
    idx, folds = _folds()
    assert folds, "no folds produced"
    for f in folds:
        train_end = idx[f["train"]].max()
        test_start = idx[f["test"]].min()
        assert train_end < test_start, f"fold {f['label']} trains on the future"


def test_folds_do_not_overlap_and_move_forward():
    idx, folds = _folds()
    starts = [idx[f["test"]].min() for f in folds]
    assert starts == sorted(starts), "folds are not in chronological order"
    for a, b in zip(folds, folds[1:]):
        assert not (a["test"] & b["test"]).any(), "test windows overlap"


def test_training_window_expands():
    idx, folds = _folds()
    sizes = [f["train"].sum() for f in folds]
    assert all(b > a for a, b in zip(sizes, sizes[1:])), "training set should grow"


def test_tuning_folds_come_first_and_reporting_folds_are_later():
    """Tune on history, report on the future — never the other way round."""
    idx, folds = _folds()
    tune = [f for f in folds if f["purpose"] == "tune"]
    report = [f for f in folds if f["purpose"] == "report"]
    assert len(tune) == ev.TUNE_FOLDS
    assert report, "no reporting folds left"
    latest_tune = max(idx[f["test"]].max() for f in tune)
    earliest_report = min(idx[f["test"]].min() for f in report)
    assert latest_tune < earliest_report


# ------------------------------------------------------------------- scoring
def test_per_hour_mae_and_rmse():
    y = np.array([10.0, 20.0, 30.0])
    p = np.array([12.0, 20.0, 25.0])          # errors +2, 0, -5
    s = ev.per_hour(y, p)
    assert s["MAE"] == pytest.approx(7 / 3)
    assert s["RMSE"] == pytest.approx(np.sqrt((4 + 0 + 25) / 3))


def test_per_day_reports_a_distribution_not_just_a_mean():
    # start at local midnight (Stockholm is UTC+1 in January) so the 48 hours
    # are exactly two local days — per_day groups by LOCAL date
    idx = hourly("2024-12-31 23:00", 48)
    y = np.zeros(48)
    p = np.concatenate([np.full(24, 1.0), np.full(24, 9.0)])   # day1 off by 1, day2 by 9
    d = ev.per_day(y, p, idx)
    assert d["n_days"] == 2
    assert d["mean_day_MAE"] == pytest.approx(5.0)
    assert d["worst_day_MAE"] == pytest.approx(9.0)
    # the spread is the point: a bad day is far worse than the average day
    assert d["worst_day_MAE"] > d["mean_day_MAE"]


def test_per_day_mean_matches_per_hour_when_days_are_equal_length():
    idx = hourly("2024-12-31 23:00", 48)
    rng = np.random.default_rng(0)
    y = rng.normal(50, 10, 48)
    p = y + rng.normal(0, 5, 48)
    assert ev.per_day(y, p, idx)["mean_day_MAE"] == pytest.approx(ev.per_hour(y, p)["MAE"])


# ---------------------------------------------------------------- noise rule
def test_bootstrap_detects_a_real_difference():
    idx = hourly("2025-01-01", 24 * 60)
    rng = np.random.default_rng(0)
    y = rng.normal(50, 10, len(idx))
    good = y + rng.normal(0, 1, len(idx))
    bad = y + rng.normal(0, 12, len(idx))
    cmp = ev.bootstrap_difference(y, bad, good, idx, n_boot=400)
    assert cmp["significant"] and cmp["diff"] > 0
    assert ev.verdict(cmp) == "better"


def test_bootstrap_calls_a_coin_flip_a_coin_flip():
    idx = hourly("2025-01-01", 24 * 60)
    rng = np.random.default_rng(1)
    y = rng.normal(50, 10, len(idx))
    a = y + rng.normal(0, 5, len(idx))
    b = y + rng.normal(0, 5, len(idx))          # same quality, different noise
    cmp = ev.bootstrap_difference(y, a, b, idx, n_boot=400)
    assert not cmp["significant"]
    assert ev.verdict(cmp) == "not distinguishable from noise"


def test_bootstrap_resamples_whole_days_not_hours():
    """Errors that cluster by day must be treated as ~30 observations, not ~720.

    A is better on half the days and worse on the other half, consistently
    within each day, netting out to roughly zero. Resampling individual hours
    would see ~720 independent observations and call the wobble significant;
    resampling whole days sees only 30, and correctly reports uncertainty.
    """
    idx = hourly("2024-12-31 23:00", 24 * 30)
    rng = np.random.default_rng(2)
    y = rng.normal(50, 10, len(idx))
    day_effect = np.repeat(rng.choice([-5.0, 5.0], 30), 24)
    a = y + day_effect          # whole days good or bad together
    b = y - day_effect
    cmp = ev.bootstrap_difference(y, a, b, idx, n_boot=600)
    assert not cmp["significant"], (
        f"day-clustered noise was called real: {cmp}")


# --------------------------------------------------------------- calibration
def test_interval_coverage_counts_what_falls_inside():
    y = np.array([1.0, 5.0, 9.0, 12.0])
    lo = np.array([0.0, 0.0, 0.0, 0.0])
    hi = np.array([10.0, 10.0, 10.0, 10.0])
    c = ev.interval_coverage(y, lo, hi)
    assert c["coverage"] == pytest.approx(0.75)   # 12 falls outside
    assert c["above"] == pytest.approx(0.25)
    assert c["below"] == 0.0


def test_pinball_loss_penalises_the_correct_side():
    y = np.array([10.0])
    # for the 10th percentile, over-predicting should hurt more than under
    over = ev.pinball_loss(y, np.array([12.0]), 0.1)
    under = ev.pinball_loss(y, np.array([8.0]), 0.1)
    assert over > under


# --------------------------------------------------- early-stopping criterion
def test_early_stopping_scores_a_point_model_with_mae():
    y = np.array([10.0, 20.0])
    pred = np.array([12.0, 20.0])
    assert _validation_score(y, pred, {}) == pytest.approx(1.0)


def test_early_stopping_scores_a_quantile_model_with_pinball():
    """A q90 model judged by MAE stops where it is least like a q90 model.

    This is the bug that made the first 80% intervals cover 57.9%.
    """
    y = np.array([10.0, 20.0])
    pred = np.array([12.0, 20.0])
    params = {"loss": "quantile", "quantile": 0.9}
    assert _validation_score(y, pred, params) == pytest.approx(
        ev.pinball_loss(y, pred, 0.9))
    # and it must not be the MAE it used to be
    assert _validation_score(y, pred, params) != pytest.approx(1.0)


def test_the_two_criteria_prefer_different_predictions_for_q90():
    """MAE prefers the median-ish guess; pinball at q90 prefers the high one."""
    y = np.array([0.0, 10.0, 20.0, 30.0, 100.0])
    middle, high = np.full(5, 20.0), np.full(5, 60.0)
    q90 = {"loss": "quantile", "quantile": 0.9}
    assert _validation_score(y, middle, {}) < _validation_score(y, high, {})
    assert _validation_score(y, high, q90) < _validation_score(y, middle, q90)


# ------------------------------------------------- asymmetric conformal pads
class _SkewedQuantile:
    """Interval is right about the floor but far too low at the ceiling."""

    def __init__(self, loss=None, quantile=None, **kw):
        self.quantile = quantile

    def fit(self, train):
        return self

    def predict(self, test):
        return np.full(len(test), {0.1: -2.0, 0.5: 0.0, 0.9: 2.0}[self.quantile])


def test_symmetric_conformal_pads_both_sides_equally():
    from models.predictors import ConformalQuantileModel as C
    y = np.tile([-2.0, 20.0], 500)                # misses badly only above
    train = pd.DataFrame({"target_spot_price_se3": y, "x": 1.0},
                         index=hourly("2025-01-01", len(y)))
    m = C(_SkewedQuantile, cal_frac=0.4, symmetric=True).fit(train)
    assert m.pad_lo_ == m.pad_hi_, "symmetric mode must use one pad"


def test_asymmetric_conformal_gives_the_skewed_side_more_room():
    """Prices are right-skewed, so the ceiling needs more padding than the floor.

    One symmetric pad necessarily overshoots one side and undershoots the other;
    this is the fix for the 5.5%-below / 14.1%-above split measured in stage 5.
    """
    from models.predictors import ConformalQuantileModel as C
    y = np.tile([-2.0, 20.0], 500)
    train = pd.DataFrame({"target_spot_price_se3": y, "x": 1.0},
                         index=hourly("2025-01-01", len(y)))
    m = C(_SkewedQuantile, cal_frac=0.4, symmetric=False).fit(train)
    assert m.pad_hi_ > m.pad_lo_, "the over-shooting side must get the bigger pad"
    pred = m.predict(train.iloc[:4])
    assert pred[0.9][0] >= 20.0, "ceiling must reach the high outcomes"
    assert pred[0.1][0] > -20.0, "floor must not be padded needlessly"


# ------------------------------------------------------------ linear control
def _feature_frame(n: int = 300, missing: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    price = rng.normal(50, 20, n)
    big = price * 1000.0                       # a feature on a wildly different scale
    small = np.sin(np.arange(n))               # a cyclical encoding in [-1, 1]
    if missing:
        big[::7] = np.nan                      # genuine weather-style gaps
    return pd.DataFrame(
        {"big": big, "small": small,
         "target_spot_price_se3": price + rng.normal(0, 1, n)},
        index=hourly("2025-01-01", n))


def test_ridge_trains_and_predicts_with_missing_values():
    """The real feature table has gaps in 10 weather columns; ridge cannot take
    a NaN, so the imputer must be doing its job inside the pipeline."""
    train = _feature_frame(300, missing=True)
    test = _feature_frame(50, missing=True)
    pred = LinearModel(alpha=10.0).fit(train).predict(test)
    assert len(pred) == 50
    assert np.isfinite(pred).all(), "a NaN reached the model"


def test_ridge_imputes_from_the_training_fold_only():
    """Filling a gap with a median computed over train *and* test would leak the
    future into the past — the same class of bug as a random validation split."""
    train = _feature_frame(300, missing=True)
    m = LinearModel(alpha=10.0).fit(train)
    imputer = m.model_.named_steps["simpleimputer"]
    expected = np.nanmedian(train["big"].to_numpy())
    assert imputer.statistics_[0] == pytest.approx(expected)


def test_ridge_is_scaled_so_units_do_not_decide_the_penalty():
    """Ridge penalises each coefficient equally, so without standardising, a
    feature measured in thousands would be penalised out of the model purely
    because of its units. Same data, same signal, different scale -> same fit."""
    train = _feature_frame(400)
    test = _feature_frame(60)
    a = LinearModel(alpha=10.0).fit(train).predict(test)

    scaled_train, scaled_test = train.copy(), test.copy()
    scaled_train["big"] = scaled_train["big"] / 1e6
    scaled_test["big"] = scaled_test["big"] / 1e6
    b = LinearModel(alpha=10.0).fit(scaled_train).predict(scaled_test)

    assert np.allclose(a, b, atol=1e-6), "predictions changed with feature units"


# ------------------------------------------------------- conformal intervals
class _FakeQuantile:
    """A stand-in model with a deliberately too-narrow interval.

    Predicts a constant per quantile, so the conformal padding is arithmetic
    that can be checked by hand rather than a property of gradient boosting.
    """

    def __init__(self, loss=None, quantile=None, **kw):
        self.quantile = quantile

    def fit(self, train):
        return self

    def predict(self, test):
        return np.full(len(test), {0.1: -1.0, 0.5: 0.0, 0.9: 1.0}[self.quantile])


def _cal_frame(y: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({"target_spot_price_se3": y, "x": 1.0},
                        index=hourly("2025-01-01", len(y)))


def test_conformal_widens_an_overconfident_interval():
    """Truth sits at +-5 but the model claims +-1; the padding must close that."""
    train = _cal_frame(np.tile([-5.0, 5.0], 500))
    m = ConformalQuantileModel(_FakeQuantile, cal_frac=0.4).fit(train)
    assert m.pad_ > 0, "an interval that misses every row must be widened"

    pred = m.predict(train.iloc[:10])
    assert pred[0.9][0] > 1.0 and pred[0.1][0] < -1.0
    # -1 - pad <= -5 and 5 <= 1 + pad, i.e. the interval now reaches the data
    assert pred[0.9][0] >= 5.0 and pred[0.1][0] <= -5.0


def test_conformal_shrinks_an_over_wide_interval():
    """The correction is signed: a needlessly wide interval must narrow."""
    train = _cal_frame(np.zeros(1000))          # truth always dead centre
    m = ConformalQuantileModel(_FakeQuantile, cal_frac=0.4).fit(train)
    assert m.pad_ < 0, "an interval far wider than needed must be narrowed"


def test_conformal_calibration_slice_is_the_most_recent_rows():
    """Calibration must come from the end of the training period, not the start.

    Using old rows would calibrate the interval to a regime that has passed --
    the exact failure this correction exists to fix.
    """
    y = np.concatenate([np.zeros(800), np.tile([-9.0, 9.0], 100)])
    m = ConformalQuantileModel(_FakeQuantile, cal_frac=0.2).fit(_cal_frame(y))
    # the last 200 rows are the volatile ones; padding must reflect them
    assert m.n_cal_ == 200
    assert m.pad_ == pytest.approx(8.0)          # 9 - 1


# ------------------------------------------------------------- hour grouping
def test_every_hour_belongs_to_exactly_one_group():
    assert sorted(ev.GROUP_OF_HOUR) == list(range(24))
    counted = sum(len(v) for v in ev.HOUR_GROUPS.values())
    assert counted == 24, "hour groups must partition the day"


def test_evening_peak_is_its_own_group():
    """The single model was weakest at 19:00-20:00; those must not be split up."""
    assert ev.GROUP_OF_HOUR[19] == ev.GROUP_OF_HOUR[20] == "evening"
    assert ev.GROUP_OF_HOUR[13] == "midday"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
