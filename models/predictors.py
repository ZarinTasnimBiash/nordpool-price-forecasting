"""Stage 5 — the predictors themselves.

Four things that can produce a forecast, from trivial to trained:

  naive_lag_24h / naive_lag_168h   no fitting at all; read a column
  train_mean                       the constant guess
  LinearModel                      ridge on the same features — the control
                                   that says how much of the gain is the
                                   algorithm and how much is the feature work
  SingleModel                      one gradient-boosting model for all hours
  GroupedModel                     one model per hour-group (night/morning/
                                   midday/evening), because the single model is
                                   weakest exactly at the peaks

EARLY STOPPING, DONE CHRONOLOGICALLY
------------------------------------
scikit-learn's built-in early stopping carves out a *random* validation slice.
For a time series that is wrong twice over: neighbouring hours correlate ~0.95,
so a random slice leaks near-duplicates of the training rows, and the split is
not reproducible between runs. It was disabled earlier in this project for
exactly that reason.

Here the validation slice is the **last stretch of the training period, in time
order**. The number of trees is grown with `warm_start`, checking validation
error as it goes, and training stops when it has not improved for `patience`
rounds. That is genuine early stopping — it just uses the future-facing split a
time series requires.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from models.evaluation import GROUP_OF_HOUR, HOUR_GROUPS, LOCAL_TZ, pinball_loss

DROP_COLS = ["target_spot_price_se3", "cutoff_utc"]
TARGET = "target_spot_price_se3"

BASE_PARAMS = dict(
    learning_rate=0.05,
    max_leaf_nodes=31,
    min_samples_leaf=40,
    l2_regularization=1.0,
    early_stopping=False,      # done here instead, chronologically
    random_state=0,
)


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in DROP_COLS]


# ---------------------------------------------------------------- baselines
def naive(df: pd.DataFrame, column: str) -> np.ndarray:
    """A 'model' that just reads a lag column. No fitting."""
    return df[column].to_numpy(float)


def train_mean(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    return np.full(len(test), train[TARGET].mean(), dtype=float)


# ----------------------------------------------------- chronological ES
def _validation_score(y_true: np.ndarray, y_pred: np.ndarray, params: dict) -> float:
    """Score the validation slice with the loss the model is actually fitting.

    A quantile model is not trying to be close on average, it is trying to sit
    at (say) the 90th percentile, so MAE is the wrong ruler for it: MAE is
    minimised by the median, and judging a q90 model by MAE stops it at the
    point where it is *least* like a q90 model. Every quantile model in the
    first Stage 5 run stopped at the wrong number of trees for that reason,
    and the 80% intervals came out at 57.9%.
    """
    if params.get("loss") == "quantile":
        return pinball_loss(y_true, y_pred, params["quantile"])
    return float(np.abs(y_pred - y_true).mean())


def _fit_with_early_stopping(X: pd.DataFrame, y: np.ndarray, params: dict,
                             val_frac: float = 0.15, step: int = 25,
                             max_iter: int = 800, patience: int = 4):
    """Grow trees until a time-ordered validation slice stops improving."""
    n_val = max(200, int(len(X) * val_frac))
    if n_val >= len(X):                       # too little data to hold out
        model = HistGradientBoostingRegressor(max_iter=200, **params)
        model.fit(X, y)
        return model, 200

    X_tr, y_tr = X.iloc[:-n_val], y[:-n_val]
    X_va, y_va = X.iloc[-n_val:], y[-n_val:]

    probe = HistGradientBoostingRegressor(max_iter=step, warm_start=True, **params)
    best_score, best_n, since_best = np.inf, step, 0
    n = step
    while n <= max_iter:
        probe.set_params(max_iter=n)
        probe.fit(X_tr, y_tr)
        score = _validation_score(y_va, probe.predict(X_va), params)
        if score < best_score - 1e-9:
            best_score, best_n, since_best = score, n, 0
        else:
            since_best += 1
            if since_best >= patience:
                break
        n += step

    # refit on the full training period with the chosen number of trees
    final = HistGradientBoostingRegressor(max_iter=best_n, **params)
    final.fit(X, y)
    return final, best_n


# ------------------------------------------------------------------ models
class SingleModel:
    """One model for all 24 hours."""

    name = "single"

    def __init__(self, **overrides):
        self.params = {**BASE_PARAMS, **overrides}
        self.n_trees_ = None

    def fit(self, train: pd.DataFrame):
        cols = feature_columns(train)
        self.cols_ = cols
        self.model_, self.n_trees_ = _fit_with_early_stopping(
            train[cols], train[TARGET].to_numpy(float), self.params)
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return self.model_.predict(test[self.cols_])


class LinearModel:
    """Ridge regression on the same 70 features — the control for the whole stage.

    Without this, "gradient boosting beats the naive baseline by 25.6%" is
    ambiguous: it could be the algorithm, or it could be the feature
    engineering, which *any* model would have benefited from. A linear model on
    identical inputs separates the two. If ridge lands near the boosted model,
    most of the gain was the features and the choice of algorithm barely
    mattered; if it lands near the naive baseline, the algorithm is doing the
    work.

    Two things gradient boosting handled for free have to be done explicitly
    here, and both are fitted inside the pipeline so they only ever see the
    training fold:

      impute  10 weather columns have real gaps (up to 13.1%, genuine SMHI
              outages). Trees learn a direction to send a missing value; ridge
              cannot take one at all. Median of the training fold.
      scale   features range from prices in the hundreds to sine encodings in
              [-1, 1]. Ridge's penalty is applied per coefficient, so without
              standardising, the penalty would fall almost entirely on the
              small-scale features purely because of their units.

    Ridge rather than plain least squares because the features are heavily
    collinear by construction — `spot_price_se3_lag_24h` and
    `spot_price_se3_roll_24h_mean` describe nearly the same thing, and
    unpenalised regression responds to that with huge cancelling coefficients.
    """

    name = "ridge"

    def __init__(self, alpha: float = 10.0):
        self.alpha = alpha

    def fit(self, train: pd.DataFrame):
        self.cols_ = feature_columns(train)
        self.model_ = make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            Ridge(alpha=self.alpha),
        )
        self.model_.fit(train[self.cols_], train[TARGET].to_numpy(float))
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        return self.model_.predict(test[self.cols_])


class GroupedModel:
    """One model per hour-group — night, morning, midday, evening.

    Deliberately four groups rather than 24 separate hourly models: at 20,900
    rows, one model per hour would see roughly 870 rows against 70 features,
    which invites memorisation. Four groups give ~5,200 rows each while still
    letting the evening peak be modelled differently from the small hours.
    """

    name = "grouped"

    def __init__(self, **overrides):
        self.params = {**BASE_PARAMS, **overrides}
        self.n_trees_ = {}

    @staticmethod
    def _groups(df: pd.DataFrame) -> np.ndarray:
        hours = df.index.tz_convert(LOCAL_TZ).hour
        return np.array([GROUP_OF_HOUR[h] for h in hours])

    def fit(self, train: pd.DataFrame):
        cols = feature_columns(train)
        self.cols_ = cols
        g = self._groups(train)
        self.models_ = {}
        for name in HOUR_GROUPS:
            part = train[g == name]
            model, n = _fit_with_early_stopping(
                part[cols], part[TARGET].to_numpy(float), self.params)
            self.models_[name] = model
            self.n_trees_[name] = n
        return self

    def predict(self, test: pd.DataFrame) -> np.ndarray:
        g = self._groups(test)
        out = np.empty(len(test), dtype=float)
        for name, model in self.models_.items():
            mask = g == name
            if mask.any():
                out[mask] = model.predict(test.loc[mask, self.cols_])
        return out


class QuantileModel:
    """Three models — lower, median, upper — giving a range instead of a point.

    A point forecast whose typical miss is a third of the price level invites
    false confidence. An interval says what the model actually knows. Whether
    the interval is honest is checked separately, by measuring how often the
    truth really falls inside it.
    """

    def __init__(self, base_cls, quantiles=(0.1, 0.5, 0.9), **overrides):
        self.base_cls = base_cls
        self.quantiles = quantiles
        self.overrides = overrides

    def fit(self, train: pd.DataFrame):
        self.models_ = {}
        for q in self.quantiles:
            m = self.base_cls(loss="quantile", quantile=q, **self.overrides)
            self.models_[q] = m.fit(train)
        return self

    def predict(self, test: pd.DataFrame) -> dict:
        return {q: m.predict(test) for q, m in self.models_.items()}


class ConformalQuantileModel:
    """QuantileModel with the interval width corrected against held-out data.

    The quantile models alone are overconfident: they claim 80% and deliver
    70.3%. That is not a bug, it is what quantile regression does — the
    quantiles are fitted on training data, so they describe how wrong the model
    was on rows it had already seen, which is less wrong than it will be
    tomorrow.

    The correction (conformalized quantile regression, Romano et al. 2019):

      1. hold back the last stretch of the training period, in time order;
      2. fit the quantile models on everything before it;
      3. on the held-back slice, ask how far outside the interval the truth
         actually fell — `max(lower - y, y - upper)`, negative when inside;
      4. take the (1-alpha) empirical quantile of those misses and pad the
         interval by it, on both sides.

    Step 3 is the whole idea: rather than trusting the interval, measure how
    much it was missing by on data the model never saw, and add exactly that
    much back. If the interval was already too wide the padding is negative and
    it shrinks.

    The usual guarantee assumes exchangeable data, which hourly prices are not.
    The calibration slice is therefore the *most recent* stretch before the
    forecast, so the padding reflects the current regime rather than the
    average of three years.
    """

    def __init__(self, base_cls, lo_q: float = 0.1, hi_q: float = 0.9,
                 cal_frac: float = 0.15, symmetric: bool = True, **overrides):
        self.base_cls = base_cls
        self.lo_q, self.hi_q = lo_q, hi_q
        self.cal_frac = cal_frac
        self.symmetric = symmetric
        self.overrides = overrides
        self.alpha = 1.0 - (hi_q - lo_q)          # 0.2 for an 80% interval

    def fit(self, train: pd.DataFrame):
        n_cal = max(200, int(len(train) * self.cal_frac))
        proper, cal = train.iloc[:-n_cal], train.iloc[-n_cal:]

        self.inner_ = QuantileModel(
            self.base_cls, quantiles=(self.lo_q, 0.5, self.hi_q),
            **self.overrides).fit(proper)

        pr = self.inner_.predict(cal)
        y = cal[TARGET].to_numpy(float)
        self.n_cal_ = len(y)

        if self.symmetric:
            # One score per row: how far outside the interval the truth fell,
            # whichever side it missed on. One pad, applied to both ends.
            scores = np.maximum(pr[self.lo_q] - y, y - pr[self.hi_q])
            self.pad_lo_ = self.pad_hi_ = self._conformal_q(scores, self.alpha)
        else:
            # Electricity prices are not symmetric — SE3 reaches EUR 707 but
            # cannot fall below about -60 — so one pad necessarily overshoots
            # the floor and undershoots the ceiling. Here each side gets its own
            # pad and half the miscoverage budget: at most alpha/2 of outcomes
            # may fall below, and at most alpha/2 above. The two events are
            # disjoint, so total coverage still lands at 1 - alpha.
            self.pad_lo_ = self._conformal_q(pr[self.lo_q] - y, self.alpha / 2)
            self.pad_hi_ = self._conformal_q(y - pr[self.hi_q], self.alpha / 2)
        self.pad_ = float((self.pad_lo_ + self.pad_hi_) / 2)   # for reporting
        return self

    @staticmethod
    def _conformal_q(scores: np.ndarray, alpha: float) -> float:
        """The (1-alpha) conformal quantile, with the finite-sample correction.

        The (n+1)/n factor matters at these calibration sizes and is what makes
        the coverage guarantee exact rather than approximate.
        """
        k = min(1.0, np.ceil((len(scores) + 1) * (1 - alpha)) / len(scores))
        return float(np.quantile(scores, k, method="higher"))

    def predict(self, test: pd.DataFrame) -> dict:
        pr = self.inner_.predict(test)
        return {self.lo_q: pr[self.lo_q] - self.pad_lo_,
                0.5: pr[0.5],
                self.hi_q: pr[self.hi_q] + self.pad_hi_}
