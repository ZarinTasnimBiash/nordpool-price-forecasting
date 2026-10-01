# Stage 5 — Modelling

Run with `PYTHONPATH=. OMP_NUM_THREADS=1 python -u models/run_stage5.py`. Output:
`reports/stage5_results.json`. Takes roughly 50 minutes.

**Headline: the model beats the naive baseline by 6.7 EUR/MWh (25.6%) on ten months that
were never used for tuning, and it wins on every one of those ten folds.** But a ridge
regression on the same features gets 6.6 of that 6.7 — so the win belongs to the feature
engineering, not to gradient boosting (section 6). The prediction intervals are a separate
story again, told in section 7.

## 1. What is being predicted, and against what

At 11:00 on day D, predict the 24 hourly SE3 prices for day D+1. The feature table from
Stage 4 supplies 20,900 rows × 70 features, spanning 2024-04-01 → 2026-08-19.

Three baselines, none of them strawmen:

| Baseline | How | Why it is fair |
|---|---|---|
| **yesterday, same hour** | `spot_price_se3_lag_24h` used directly | lag-24 autocorrelation is 0.677; this is what a trader does in their head |
| last week, same hour | `spot_price_se3_lag_168h` used directly | the weekly cycle is real (r rises 0.427 → 0.450 from 72h to 168h) |
| the training mean | a constant | shows what "no information" scores |

The first is the number to beat. It requires no data pipeline at all, so any model that does
not clear it comfortably has not earned its existence.

## 2. Why walk-forward, and not a single holdout

Stage 4 used one chronological split. Its test window turned out not to resemble its training
window at all:

| | n | mean | median | sd | p95 | % negative |
|---|---|---|---|---|---|---|
| train | 16,556 | 43.8 | 31.2 | 45.6 | 130.2 | 5.7% |
| test | 4,344 | 58.9 | 57.9 | 40.1 | 128.0 | 1.5% |

The test period's median is nearly **double** the training median, and negative hours almost
disappear. That is a change of regime, not a sampling wobble. A single score measured on that
window says as much about the window as about the model.

So: **expanding-window folds**. Train on everything before, test the next calendar month,
roll forward.

```
MIN_TRAIN_MONTHS    = 14      # a fold must see at least one full winter
TEST_MONTHS_PER_FOLD = 1
TUNE_FOLDS          = 5
```

**15 folds — the first 5 for tuning (2025-06 → 2025-10), the remaining 10 untouched for
reporting (2025-11 → 2026-08).**

The vindication is in the baseline itself. Across the reporting folds, the naive baseline's
own MAE ranges from **21.1 to 33.7**; on the tuning folds it ranges from **14.0 to 35.8**.
A single fold could have reported almost anything. Note also that the training-mean baseline
degrades from 27.0 on the tuning folds to 39.9 on the reporting folds — prices rose, so a
historical average became a worse guess. Any single-split result would have inherited that
drift silently.

**Baselines across the folds:**

| Predictor | Folds | MAE | RMSE | fold MAE mean | sd | min | max | median day | p90 day |
|---|---|---|---|---|---|---|---|---|---|
| naive: yesterday same hour | tuning (5) | 22.00 | 32.89 | 21.96 | 7.65 | 14.0 | 35.8 | 16.83 | 44.09 |
| naive: last week same hour | tuning (5) | 26.45 | 39.36 | 26.41 | 9.81 | 13.8 | 41.1 | 20.98 | 48.36 |
| naive: training mean | tuning (5) | 27.03 | 38.72 | 27.01 | 7.20 | 18.2 | 38.7 | 22.83 | 47.34 |
| **naive: yesterday same hour** | **report (10)** | **26.28** | **36.90** | **26.30** | **3.12** | **21.1** | **33.7** | **23.05** | **46.93** |
| naive: last week same hour | report (10) | 34.63 | 47.80 | 34.86 | 4.49 | 25.5 | 41.8 | 27.47 | 71.76 |
| naive: training mean | report (10) | 39.89 | 51.93 | 40.21 | 13.26 | 25.2 | 64.8 | 32.86 | 74.20 |

## 3. Two scorings

**Per hour** — conventional, and comparable to published work.

**Per forecast-day** — the 24 hourly errors averaged into one number per day, then reported
as a *distribution*. Operationally one decision is made at 11:00 covering 24 hours, so a day
is the natural unit.

The per-day *mean* is essentially identical to the per-hour MAE — the same errors, regrouped.
What the day view adds is the **spread**: for the naive baseline, median day 23.05 against
p90 day 46.93 and a worst day of 99.5. An average hides how bad a bad day is.

## 4. The noise rule, instead of a success threshold

A fixed target ("beat 20 EUR/MWh") was considered and rejected as circular — any threshold I
could set would be set just under what I had already achieved. The rule used instead:

> Keep a change only when its improvement survives a **block bootstrap over whole days**
> across the reporting folds. Stop when new changes are no longer distinguishable from noise.

**Whole days, never individual hours.** Neighbouring hours correlate about 0.95; resampling
hours independently would pretend there is far more independent evidence than there is, and
would make almost everything look significant. 2,000 resamples, 95% percentile interval.

## 5. Early stopping, done chronologically

scikit-learn's `HistGradientBoostingRegressor` enables early stopping automatically above
10,000 rows, and carves out a **random** validation slice to do it. For a time series that is
wrong twice over: a random slice contains near-duplicates of the training rows (0.95
correlation between neighbours), and the split is not reproducible between runs. It was
disabled earlier in this project for exactly that reason.

`models/predictors.py::_fit_with_early_stopping` reimplements it properly:

1. hold out the **last 15% of the training period in time order** — the future-facing split a
   time series requires;
2. grow trees in steps of 25 up to 800 using `warm_start`, scoring the held-out slice each
   time;
3. stop after 4 steps without improvement;
4. **refit on the full training period** with the chosen number of trees, so nothing is
   wasted.

Verified working, measured by `models/lr_sweep.py`: 0.02 stops at 535 trees, 0.03 at 305, 0.05 at
215, 0.08 at 170, 0.20 at 60 — well short of the 800 ceiling, and identical between runs.

### Does the learning rate matter? No

The tuning grid below varies several settings at once, so it cannot answer this: the two configs
that are not `learning_rate=0.05` each *also* change `min_samples_leaf`. That is a confound, and
"0.05 won" is evidence about a config rather than about a learning rate. `models/lr_sweep.py` holds
everything else fixed and moves only the one knob, on the same five tuning folds:

| lr | MAE | fold sd | trees grown |
|---|---|---|---|
| 0.02 | 17.18 | 3.69 | 535 |
| 0.03 | 17.07 | 3.64 | 305 |
| **0.05** | **17.14** | **3.82** | **215** |
| 0.08 | 17.04 | 3.69 | 170 |
| 0.12 | 17.47 | 4.26 | 105 |
| 0.20 | 17.11 | 3.59 | 60 |

**Across a 10× range of learning rates the MAE spread is 0.43, while fold-to-fold variation is 3.7
in every single row.** The pattern is non-monotonic — 0.12 is the worst and 0.20, nearly twice as
aggressive, is fine — which is what a flat surface with noise on top looks like, not a real effect.

The tree column is the explanation. A smaller step needs more steps: 0.02 grows 535 trees where
0.20 grows 60, nearly 9× fewer trees for 10× larger steps. **Early stopping absorbs the learning
rate almost entirely.** With the tree count fixed it would matter a great deal; letting early
stopping choose it makes the two cancel.

So `learning_rate=0.05` is justified as *inside a wide flat region*, not as optimal. It was the
default in `BASE_PARAMS` before any tuning ran, and nothing since has given a reason to move it.

Early stopping and the noise rule answer different questions and both are needed. Early
stopping decides when *one model* has trained enough. The noise rule decides which *changes*
to keep.

## 6. Tuning, then the point forecast

**Tune once on the 5 tuning folds, then freeze.** The 10 reporting folds are never seen while
choosing hyperparameters. This is simpler than nested cross-validation, much harder to get
subtly wrong, and it mirrors what deployment actually does: tune on history, run forward.

Six configurations, varying learning rate, tree size, leaf minimum and L2:

**Chosen: `learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=80`, tuning MAE 17.14.**

Tuning barely mattered — all six configurations landed between **17.14 and 17.36**, a spread
of 0.22 against fold-to-fold variation of several whole units. The chosen one is the most
constrained of the six, which is mildly reassuring, but the honest conclusion is that this
model is not sensitive to these knobs and further tuning would be effort spent on noise.

### Results on the ten untouched folds

| Predictor | MAE | RMSE | fold MAE mean | sd | min | max | median day | p90 day |
|---|---|---|---|---|---|---|---|---|
| naive: yesterday same hour | 26.28 | 36.90 | 26.30 | 3.12 | 21.1 | 33.7 | 23.05 | 46.93 |
| naive: last week same hour | 34.63 | 47.80 | 34.86 | 4.49 | 25.5 | 41.8 | 27.47 | 71.76 |
| naive: training mean | 39.89 | 51.93 | 40.21 | 13.26 | 25.2 | 64.8 | 32.86 | 74.20 |
| **model: single** | **19.56** | **26.73** | **19.66** | **1.92** | **16.5** | **22.2** | **16.80** | **31.59** |
| model: grouped | 19.83 | 27.00 | 19.94 | 2.36 | 16.9 | 23.5 | 17.32 | 34.39 |

Under the noise rule:

| Comparison | Δ MAE | 95% CI | Verdict |
|---|---|---|---|
| **single model vs best naive** | **+6.72** | **[+5.13, +8.42]** | **real** |
| grouped model vs best naive | +6.44 | [+4.87, +8.21] | real |
| grouped vs single | −0.28 | [−0.79, +0.25] | not distinguishable from noise |

**+6.72 EUR/MWh is a 25.6% improvement**, and it is not carried by a few lucky months.

Two things in that table matter as much as the headline:

- **The model is more stable than the baseline it beats.** Fold-to-fold standard deviation
  1.92 against 3.12, and a fold range of 16.5–22.2 against 21.1–33.7. It does not merely win
  on average; it wins across regimes, and its spread is narrower than the thing it is beating.
- **Bad days improve more than typical days.** p90 day falls from 46.93 to 31.59 (−33%) while
  the median day falls from 23.05 to 16.80 (−27%). The model helps most where the baseline
  hurts most.

### Fold by fold

An average can hide a model that wins hugely in three months and loses in seven. It does not
here:

| Fold | naive | single | gain | | Fold | naive | single | gain |
|---|---|---|---|---|---|---|---|---|
| 2025-11 | 33.69 | 21.05 | **+37.5%** | | 2026-04 | 25.81 | 18.56 | +28.1% |
| 2025-12 | 24.44 | 18.71 | +23.4% | | 2026-05 | 26.15 | 20.58 | +21.3% |
| 2026-01 | 24.89 | 22.22 | +10.7% | | 2026-06 | 29.29 | 20.49 | +30.1% |
| 2026-02 | 25.44 | 20.06 | +21.1% | | 2026-07 | 21.06 | 16.52 | +21.6% |
| 2026-03 | 25.96 | 16.52 | +36.3% | | 2026-08 | 26.29 | 21.84 | +16.9% |

**The model wins on all ten folds, by between 10.7% and 37.5%.** The weakest month is
2026-01 — midwinter, the hardest and most valuable period — which is worth carrying into
Stage 6.

### The grouped model: a clean negative result

Four models by hour-group — `night` (23, 0–5), `morning` (6–10), `midday` (11–16), `evening`
(17–22) — with groups taken from the Stage 3 hour profile and the Stage 4 error-by-hour
breakdown, not chosen arbitrarily. The motivation was real: error at the evening peak is
nearly double the midday error.

It did not work. −0.28 with an interval spanning zero, a *higher* fold-to-fold spread, and
a win only 6 times out of 10 against the single model — which is what "not distinguishable
from noise" looks like when you go and check it fold by fold.
The evening-peak weakness is genuine, but splitting the training data four ways costs more
than the specialisation gains. **The code is kept so the null result stays reproducible.**

Twenty-four separate hourly models were considered and rejected before running: 20,900 ÷ 24 ≈
870 rows against 70 features invites memorisation. Four groups give ~5,200 rows each. If four
groups already lose, twenty-four would lose by more.

### The control: how much of this is gradient boosting?

"Gradient boosting beats the naive baseline by 25.6%" is ambiguous as it stands. It could be the
algorithm, or it could be the feature engineering — which *any* model would have benefited from.
Nothing in the stage as originally run separated those two.

So: **ridge regression on the identical 70 features**, tuned with the same discipline (alpha chosen
on the same 5 tuning folds, frozen before the reporting folds were touched).

Two things gradient boosting handled for free had to be done explicitly, both fitted inside the
pipeline so they only ever see the training fold:

- **Imputation.** Ten weather columns have real gaps, up to 13.1% — genuine SMHI outages. Trees
  learn a direction to send a missing value; ridge cannot accept one at all. Median of the training
  fold. (Computing that median over train *and* test would leak the future into the past — the same
  class of bug as the random validation split removed earlier. There is a test for it.)
- **Standardisation.** Features run from prices in the hundreds to sine encodings in [−1, 1]. Ridge
  penalises each coefficient equally, so without scaling the penalty would fall on features purely
  because of their units.

Ridge rather than ordinary least squares because the features are collinear by construction —
`spot_price_se3_lag_24h` and `spot_price_se3_roll_24h_mean` describe nearly the same thing, and
unpenalised regression answers that with huge cancelling coefficients. Chosen alpha: **1000**, with
the grid deliberately extended past it on both sides (3000 → 17.93, 10000 → 18.53), because a best
value sitting at the edge of its grid is a statement about the grid, not about the model.

| Predictor | MAE | RMSE | fold MAE sd | fold min | fold max | median day | p90 day |
|---|---|---|---|---|---|---|---|
| naive: yesterday same hour | 26.28 | 36.90 | 3.12 | 21.1 | 33.7 | 23.05 | 46.93 |
| **model: ridge** | **19.67** | 27.31 | 3.13 | 14.4 | 24.2 | 16.79 | 34.42 |
| model: single (boosting) | 19.56 | 26.73 | 1.92 | 16.5 | 22.2 | 16.80 | 31.59 |

| Comparison | Δ MAE | 95% CI | Verdict |
|---|---|---|---|
| ridge control vs best naive | +6.61 | [+5.10, +8.20] | **real** |
| **boosting vs ridge on identical features** | **+0.11** | **[−0.72, +0.93]** | **not distinguishable from noise** |

**This reframes the stage.** Of the 6.72 EUR/MWh the model gains over the naive baseline, **6.61 is
available to a linear model on the same features.** The choice of gradient boosting is worth 0.11,
with an interval comfortably spanning zero. Almost all of the value in this project came from the
feature engineering and the leakage discipline of Stage 4 — above all the archived weather
forecasts — and almost none from the learning algorithm.

That is worth saying plainly because it is the opposite of where effort usually goes.

**Boosting is still what ships, on a different argument.** Look at the spread rather than the
centre. Ridge's fold-to-fold standard deviation is 3.13 against boosting's 1.92, its fold range is
14.4–24.2 against 16.5–22.2, and its p90 day is 34.42 against 31.59. Ridge has both the best single
fold and the worst. Boosting is not more accurate on average; it is **more consistent across
regimes and better on bad days**, which for a forecast someone bids against is the property worth
having. RMSE agrees (26.73 vs 27.31): ridge's larger misses are larger.

Keeping boosting on a stability argument is defensible. Keeping it on an accuracy argument would
not have been, and that is what this control was for.

**A caveat that was proposed here and has since been refuted.** This section originally argued
that trees cannot extrapolate — they can never predict above the highest value seen in training —
and suggested that this might explain both the model's low bias and the interval's high-side
misses, with a linear model extrapolating freely by contrast.

**Stage 6 measured it: 0 of 7,007 test hours had a true price above their fold's training
maximum.** The mechanism cannot apply. The real cause is regression to the mean — the model
over-predicts cheap hours and under-predicts expensive ones — documented in `docs/06_validation.md`
section 1. The extrapolation limit of trees is real in general; it simply is not what is happening
here.

## 7. Prediction intervals — the part that had to be fixed twice

A point forecast whose typical miss is a third of the price level invites false confidence.
An interval says what the model actually knows. The standard was set *before* running
anything, so it could not be negotiated afterwards:

> An interval that claims 80% and delivers 55% is worse than no interval at all.

Three models at q = 0.1 / 0.5 / 0.9 give the interval. The first attempt failed that
standard, and the two things wrong with it are worth more than the eventual number.

### Failure 1 — the wrong ruler for early stopping

**Stated 80%, delivered 57.9%** — 13.7% of outcomes below the interval, 28.5% above.

The asymmetry was the clue. Twice as many outcomes fell *above* the interval as below, which
says the ceiling was systematically too low rather than the whole interval being too narrow.

The cause was a real bug in my own early-stopping code. `_fit_with_early_stopping` scored its
validation slice with **MAE regardless of the loss being fitted**. MAE is minimised by the
median, so judging a q90 model by MAE stops it at the point where it is *least* like a q90
model — it rewards the model for pulling its upper quantile down towards the middle. Every
quantile model in the run stopped at the wrong number of trees.

The fix is `_validation_score`: score the validation slice with **pinball loss at the model's
own quantile**, falling back to MAE for ordinary point models. Six lines.

**Result: 57.9% → 70.3%**, and the asymmetry largely closed (13.2% below, 16.5% above).

Worth stating plainly: this was a bug I wrote, and it was invisible. Nothing crashed, the
point forecast was completely unaffected, and the intervals looked like plausible intervals.
It was caught only because coverage was measured against a number fixed in advance.

### Failure 2 — quantile regression is overconfident by construction

70.3% is much better than 57.9% and still not honest. The remaining gap is not a bug, it is
what quantile regression does. The quantiles are fitted on training rows, so they describe
how wrong the model *was on data it had already seen*, which is less wrong than it will be
tomorrow.

The correction is **conformalized quantile regression** (`ConformalQuantileModel`):

1. hold back the last 15% of the training period, in time order;
2. fit the quantile models on everything before it;
3. on that held-back slice, measure how far outside the interval the truth actually fell —
   `max(lower − y, y − upper)`, negative when the truth was comfortably inside;
4. take the 80th percentile of those misses and pad the interval by it, both sides.

Step 3 is the whole idea. Rather than trusting the interval, measure what it was missing by
on rows the model never saw, and add exactly that back. The padding is **signed**: an
interval that was already too wide would shrink.

The calibration slice is deliberately the **most recent** stretch before the forecast, not a
random sample. The usual conformal guarantee assumes exchangeable data, which hourly
electricity prices emphatically are not — and this project has already been bitten once by a
regime change. Calibrating on old rows would tune the interval to a regime that has passed.

### Result

| Interval | Stated | Actual coverage | Below | Above | Mean width | Verdict |
|---|---|---|---|---|---|---|
| quantile, MAE early stopping | 80% | **57.9%** | 13.7% | 28.5% | 39.5 | unusable |
| quantile, pinball early stopping | 80% | **70.3%** | 13.2% | 16.5% | 51.7 | overconfident |
| **+ conformal calibration** | **80%** | **80.4%** | **5.5%** | **14.1%** | **66.4** | **calibrated** |

**80.4% against a target of 80%.** The intervals are usable.

The conformal padding per fold ranged from **+1.41 to +7.48 EUR/MWh** — always positive, so
the raw quantiles were too narrow in every single month, not on average.

### Three honest caveats

1. **The interval is wide: 66.4 EUR/MWh, against a test-period mean price near 59.** That is
   not a flaw in the method, it is the measurement. Day-ahead prices genuinely are that
   uncertain 13–36 hours ahead, and the earlier, narrower intervals were narrow by being
   wrong. But it does limit how much a bidding decision can lean on them.
2. **It is still asymmetric — 5.5% below, 14.1% above.** The padding is symmetric, so it
   overcorrects the floor and undercorrects the ceiling. Prices are right-skewed (SE3 reaches
   €707 with a median near 36), so the upper tail needs more room than the lower. **A separate
   pad per side is the obvious next step** and is not done.
3. **It costs the point forecast.** Holding 15% of training back for calibration degrades the
   q50 model from MAE 19.56 to 20.40. So the two should be shipped separately: the plain
   single model for the point forecast, the conformal wrapper for the interval. Do not read
   the point forecast off the interval's midpoint.

## 8. What is deployed, and what it costs

| | Model | Score |
|---|---|---|
| Point forecast | `SingleModel`, lr 0.05 / 15 leaves / min 80 | MAE **19.56**, RMSE 26.73 |
| 80% interval | `ConformalQuantileModel` wrapping it | coverage **80.4%**, width 66.4 |
| Benchmark | yesterday, same hour | MAE 26.28 |
| Control | ridge on the same 70 features | MAE **19.67** — statistically level with boosting |

MAE 19.56 against a reporting-period mean price near 59 is **33% of the mean price**. That is
the honest headline: a 25.6% improvement on the naive baseline, on a problem that remains
hard. It is not a solved problem and the report should not imply otherwise.

## 9. Reproducibility

Every number here is reproducible bit-for-bit, which took work to achieve:

- **`OMP_NUM_THREADS=1`** — thread-count changes floating-point summation order.
- **`set threads=1`** on the DuckDB read. DuckDB's parallel `avg()` is not bitwise
  reproducible; in Stage 4 this moved model scores by ~0.08, the same size as the effects
  being measured, and an entire conclusion was drawn from noise before it was found.
- **`early_stopping=False`** in the estimator — scikit-learn's own version uses a random
  validation split, which is both non-deterministic and wrong for a time series.
- **`PYTHONPATH=.`** — the harness imports `config.configs`, and running
  `PYTHONPATH=. python models/run_stage5.py` puts `models/` on the path but not the repository root.
- `--step5` re-runs only the interval step, reusing the saved hyperparameters and winner,
  so a calibration change costs ten minutes rather than fifty.

## 10. Carried into Stage 6

1. **Error by hour of day and by season.** Stage 4 measured nearly double the error at the
   evening peak (20:00) than at midday. The grouped model was the obvious fix and it failed,
   so the question is still open.
2. **Behaviour on spikes and negative hours.** Error is broad rather than spiky — the worst
   1% of hours carry only 4.6% of total error — so spike-specific machinery is probably not
   where the remaining gains are. Worth confirming against the model rather than the baseline.
3. **Feature importance against the physical expectations from the EDA.** In particular:
   does the model rely on northern wind (`wind_speed_se1`) more than local wind, as the
   correlations say it should?
4. **Re-test the 24 observed-weather features.** They earned +0.07 with an interval spanning
   zero against the older, weaker model. The decision was to keep them and re-test once the
   model improved. It has.
5. **A separate conformal pad per side**, to fix the residual asymmetry in section 7.
