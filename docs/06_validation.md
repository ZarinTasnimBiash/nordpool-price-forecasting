# Stage 6 — Validation and error analysis

Run with `PYTHONPATH=. OMP_NUM_THREADS=1 python -u models/run_stage6.py`. Output:
`reports/stage6_results.json` and `reports/stage6_followups.json`.

Stage 5 asked *is the model good?* and answered yes, on average. This stage asks the harder
questions: **where** is it bad, is it bad for reasons that make physical sense, and does every
feature earn its place? All measurements are on the ten reporting folds — 7,007 hours,
2025-11 to 2026-08.

**Three findings here overturn conclusions written down earlier.** They are recorded as
reversals rather than quietly corrected, because the reason a claim changed is worth as much
as the claim.

## 1. The single most useful result: the model shrinks

The headline diagnostic is not an error number, it is a *bias* pattern.

| True price band | n | mean signed error | true mean |
|---|---|---|---|
| negative | 110 | **+8.18** | −1.53 |
| 0–20 | 1,130 | **+13.92** | 8.39 |
| 20–50 | 1,480 | +3.13 | 35.96 |
| 50–100 | 2,796 | −6.81 | 74.56 |
| 100–200 | 1,433 | **−19.59** | 125.91 |
| 200+ | 58 | **−97.74** | 251.88 |

Positive means the model predicted too high. **It over-predicts cheap hours and under-predicts
expensive ones**: this is regression to the mean, and it is the single mechanism behind three
symptoms that had been treated as separate problems.

- **It explains why the model adds nothing on spikes.** On the 58 hours above €200 it
  under-predicts by 98 on average — saying about 154 when the truth is 252. It is not failing
  to *model* spikes, it is actively shrinking them toward the middle.
- **It explains why the model adds nothing on negative hours** — the same effect at the other
  end, +8.18 against a true mean of −1.53.
- **It explains the interval's lopsidedness.** The centre of the prediction interval is pulled
  down on exactly the expensive hours where the outcome is most likely to be extreme, so
  outcomes escape through the ceiling more often than the floor.

The aggregate signed error is −4.50, and quoting *that* number alone is misleading: it is the
weighted average of two opposite biases, and "the model runs low" is only true above about €50.

### Two explanations that were proposed and are now refuted

**Extrapolation — dead.** Tree models cannot predict above the highest value seen in training,
and this was written into the Stage 5 documentation as a likely cause of both the low bias and
the interval asymmetry. Measured: **0 of 7,007 test hours had a true price above their fold's
training maximum.** The mechanism cannot apply. That claim has been removed everywhere it
appeared.

**Regime shift — weak.** Prices roughly doubled between early training and the test period, so
a drift explanation was plausible. Correlation between how far a test month sat above its own
training mean and that month's bias is only **−0.327**, and the counter-examples are stark:
2026-02 sat €62 above its training mean with a bias of −0.20, while 2026-08 sat €1.5 above with
a bias of −10.80. Regime shift is at most a minor contributor.

Shrinkage is a property of fitting a conditional mean under uncertainty, not a bug. But it does
bound what this model can be used for, and that belongs in the report rather than in a footnote.

## 2. Error by hour of day

| | hour | MAE |
|---|---|---|
| worst | 18:00 | 24.26 |
| | 19:00 | 23.67 |
| | 17:00 | 23.83 |
| best | 00:00 | 16.17 |
| | 23:00 | 16.77 |
| | 13:00 | 16.79 |

**Ratio worst : best = 1.50×.** Stage 4 measured close to 2× for the naive baseline, so the
model has compressed the spread as well as lowered the level — but the evening peak is still
where it is weakest, and the evening peak is where the money is.

The grouped (per-hour-block) model of Stage 5 was the obvious fix and it failed: splitting the
training data four ways cost more than the specialisation gained. Since the bias analysis above
shows the evening problem is largely *shrinkage on expensive hours* rather than a distinct
time-of-day effect, an hour-specific model was arguably never the right tool.

## 3. Error by month

Gains over the naive baseline run from **+10.7% to +37.5%**, positive in all ten months. The two
weakest are **2026-01 (+10.7%)** and **2026-08 (+16.9%)**; 2026-01 and 2026-02 are also the two
most expensive months in the record (mean price €101 and €104). Consistent with section 1: the
model has least to offer exactly when prices are high.

## 4. Feature importance, checked against the physics

Permutation importance — shuffle one column, see how much MAE rises — averaged over the ten
folds. Preferred over the tree-internal measure, which counts how often a feature was *split on*
and flatters high-cardinality columns regardless of whether the splits helped.

| Family | Summed importance |
|---|---|
| price lags | **6.685** |
| weather forecast | **4.475** |
| calendar | 2.578 |
| weather observed | 0.464 |
| price rolling | **−0.055** |

Top individual features: `spot_price_se3_lag_24h` **5.712**, `wind_speed_se3_fc2` 1.552,
`wind_speed_se4_fc2` 1.310, `day_of_week` 1.061, `hour_cos` 0.759.

**One feature carries the model.** Yesterday's same hour is worth more than every weather column
combined, which is a useful thing to know before proposing more data sources.

### The Stage 3 physics claim does not transfer

Stage 3 reported that **northern wind predicts SE3's price better than local wind**
(`wind_speed_se1` −0.260 against `wind_speed_se3` −0.239) and that finding is quoted in the EDA
report. In the fitted model the opposite holds: `wind_speed_se3_fc2` scores **1.552** against
`wind_speed_se1_fc2` **0.331** — local wind matters roughly **4.7×** more.

This is not a contradiction so much as a comparison of two different variables:

| | SE1 | SE3 |
|---|---|---|
| correlation with target, **forecast** wind (what the model uses) | −0.297 | **−0.296** |
| correlation with target, **observed 24h mean** at the cutoff | −0.118 | −0.088 |
| Stage 3 figure, contemporaneous hourly observation | −0.260 | −0.239 |

On the forecast columns the two zones are **tied**. The Stage 3 ranking was measured on
contemporaneous observed wind and simply does not carry over. The EDA report's wording has been
corrected.

A second thing falls out of that table and it is more important: **the observed 24-hour means
correlate only −0.088 to −0.118 with tomorrow's price, against −0.239/−0.260 for contemporaneous
readings.** Averaging over 24 hours and shifting to a cutoff destroys about two-thirds of the
association. That is a better explanation of why observed weather looked worthless in Stage 4
than the "redundant with price lags" story told at the time.

## 5. Do the observed-weather features earn their place? Now yes — reversing Stage 4

Stage 4 measured the 24 observed-weather columns at **+0.07, CI [−0.96, +1.14]** — indis­tin­guish­able
from zero. The decision recorded then was explicit: *keep them, re-test once the model is
stronger.* The re-test:

| | MAE |
|---|---|
| with the 24 observed-weather columns | **19.558** |
| without | 20.383 |

**Keeping them is worth +0.826, 95% CI [+0.289, +1.329] — real.** The earlier decision is
vindicated rather than reversed, and the columns stay.

### Why permutation importance and ablation disagree, and which to believe

Permutation rates the whole observed-weather family at 0.464; removing the family costs 0.826.
Both are correct measurements of different things. **Permutation shuffles one column at a time**,
so with four correlated temperature columns each looks nearly useless individually — the other
three still carry the signal. Ablating the family removes that mutual cover.

**Single-column permutation systematically understates correlated groups.** Where a decision is
about a group of features, the group must be ablated; the per-column numbers are for ranking
within a family, not for deciding whether a family survives.

## 6. The price-rolling features: a decision deliberately NOT taken

The ten rolling-price features have *negative* summed permutation importance, so the family
ablation was run. On the reporting folds:

| | MAE |
|---|---|
| with the 10 rolling features | 19.558 |
| without | **19.152** |

Dropping them appears to be worth **+0.406, CI [+0.093, +0.708]** — significant, and a free
improvement on the headline number.

**It was not adopted, for two reasons.**

First, **that measurement was made on the reporting folds**, which exist precisely so that no
modelling decision is made using them. The observed-weather ablation above says "keep", so
acting on it changes nothing and costs nothing. This one says "change", and adopting it would
mean the holdout had influenced the model — the exact contamination the tune/report split
prevents.

Second, and decisively, **the tuning folds do not agree**:

| Folds | Dropping the rolling features is worth | Verdict |
|---|---|---|
| tuning (5) | **−0.113**, CI [−0.337, +0.103] | not distinguishable from noise |
| reporting (10) | **+0.406**, CI [+0.093, +0.708] | significant |

Opposite signs. A change that helps in one period and hurts in another has not been shown to be
an improvement; it has been shown to be unstable. **The rolling features stay**, and this is
recorded as an open question rather than a finding.

## 7. Prediction intervals: one pad per side

Stage 5 shipped a conformal interval with a single pad applied to both ends. It hit 80.4%
coverage against a stated 80%, but by cancelling two errors — too tight below, too loose above.

| | coverage | below | above | imbalance | mean width |
|---|---|---|---|---|---|
| symmetric (Stage 5) | 80.4% | 5.5% | 14.1% | 8.6 pts | 66.4 |
| **asymmetric (adopted)** | **80.4%** | **8.4%** | **11.1%** | **2.7 pts** | 69.0 |

Each side now gets its own pad and half the miscoverage budget: at most 10% of outcomes may fall
below, at most 10% above. The two events are disjoint, so total coverage still lands at 80%.

**Imbalance falls by two-thirds for 2.6 EUR/MWh of extra width.** Both versions hit the target,
but the asymmetric one hits it for closer to the right reason, and it is what ships.
`ConformalQuantileModel(..., symmetric=False)`.

Note the connection to section 1: the residual imbalance is a *symptom* of shrinkage, and the
per-side pad treats the symptom. Fixing the cause would mean a model that does not shrink.

## 8. What this stage changed

| | Before | After |
|---|---|---|
| Observed weather | "earns nothing" (+0.07, CI spans zero) | **earns +0.826, real** — kept |
| Northern vs local wind | "northern wind wins" (Stage 3) | **tied** on forecast columns; model leans local |
| The low bias | attributed to tree extrapolation | **refuted** (0/7,007); it is shrinkage |
| Interval | symmetric, 5.5% / 14.1% | **asymmetric, 8.4% / 11.1%** |
| Rolling features | assumed useful | **unresolved** — folds disagree; kept |

## 9. What comes next

1. **Say plainly what the model is for.** It is reliable in the ordinary middle of the price
   distribution and adds nothing at either extreme. A bidding process that cares mainly about
   spike hours would not be well served by it as it stands.
2. **Shrinkage is the top technical lead**, not more features. Options worth testing: training
   on a variance-stabilising transform (hard — 5% of prices are negative), a two-stage model
   that predicts the level and then a correction, or simply using the q90 model when the q50
   model predicts a high price.
3. **ENTSO-E load and generation remains the largest data gap.** There is still no demand-side
   signal, and `spot_price_se3_lag_24h` carrying the model at 5.712 suggests the feature set is
   thin on genuinely new information.
4. **Resolve the rolling-features question** on data neither fold set has seen.
