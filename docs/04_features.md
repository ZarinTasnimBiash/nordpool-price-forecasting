# Stage 4 — Data Preparation & Feature Engineering

Build with `PYTHONPATH=. python features/build_features.py`. Output: table `features_se3_hourly`
in the DuckDB warehouse and `data/processed/features_se3_hourly.parquet`.

**25,822 rows × 57 columns**, spanning 2023-09-08 → 2026-08-19. One row per delivery
hour; the target is the SE3 day-ahead price for that hour.

## 1. The timing rule, and why it differs by source

The forecast for delivery day D+1 is made at **11:00 Europe/Stockholm on day D**, before
the auction closes at 12:00. What exists at that instant is not the same for every source,
and the difference decides the whole feature design:

| Source | Available at the cutoff | Safe rule |
|---|---|---|
| **Prices** | all of day D — published ~13:00 on day D−1 | lag ≥ 24h |
| **Weather** | observations only up to D 11:00 | aggregates over a window **ending at the cutoff** |

A 24-hour *weather* lag would leak: for target `D+1 18:00`, the observation at `D 18:00`
is seven hours past the cutoff. Weather features are therefore 24-hour aggregates taken
**at** the cutoff, and carry the same value for all 24 target hours of a delivery day.

That flatness is not a modelling choice — it is what "no forecast data" actually costs.
A production system would use numerical weather-model forecasts here, and such a pipeline
for exactly that. **Documented limitation, carried into Stage 6.**

The gap from cutoff to target ranges from **13 hours** (target `D+1 00:00`) to **36 hours**
(target `D+1 23:00`).

## 2. The hourly grid

Prices are stored at native resolution, so building an hourly grid needs an explicit rule:

```sql
-- expand each delivery period onto the UTC hours it covers,
-- then let the SHORTER (more specific) period win any contested hour
min(resolution_minutes) over (partition by hour_utc, area)
```

- **Quarter-hourly periods** (post 2025-10-01) are averaged into their hour — 4 periods → 1 row.
- **The DST fall-back overlap** documented in Stage 3 is resolved by the same rule. On
  2023-10-29 SE3, hour `00:00 UTC` is covered only by the 2-hour period so keeps 25.34;
  hour `01:00 UTC` is covered by both, and takes the 1-hour period's 22.35.

Result: 26,016 hours × 4 areas = 104,064 rows, **unique on (hour, area) and with no gaps** —
both asserted in code, because a gap would silently turn `.shift(24)` into a wrong lag.

## 3. Features

| Family | Count | Examples |
|---|---|---|
| Price lags | 9 | `spot_price_se3_lag_24h`, `spot_price_se4_lag_168h` |
| Rolling price | 10 | `spot_price_se3_roll_24h_mean`, `..._roll_168h_mean` |
| Spread | 1 | `spread_se4_se1_lag_24h` (north–south congestion) |
| Calendar | 10 | `hour_of_day`, `is_weekend`, `hour_sin`/`hour_cos` |
| Weather | 24 | `air_temperature_se3_24h_mean`, `wind_speed_se1_24h_mean` |
| Keys + target | 3 | `hour_utc`, `cutoff_utc`, `target_spot_price_se3` |

Choices carried directly from the Stage 3 analysis:

- **Lags at 24h, 48h and 168h** for SE3 and for SE4 and SE1. The one-week lag is included
  because autocorrelation *rises* from 0.427 at 72h to 0.450 at 168h — the weekly cycle
  reasserts itself.
- **SE4 and SE1 as neighbours**: SE4 correlates 0.876 with SE3 (closest), SE1 0.697 (most
  independent, therefore most additive).
- **Northern wind is kept for every area**, because `wind_speed_se1` predicts SE3's price
  better than SE3's own wind (−0.260 vs −0.239): Sweden's wind capacity sits in the north.
- **Calendar features are cyclical** (`sin`/`cos`), so hour 23 sits next to hour 0 rather
  than 23 units away.

Sanity check: `spot_price_se3_lag_24h` correlates **0.679** with the target, against the
0.677 lag-24 autocorrelation measured independently in Stage 3.

## 4. What the leakage audit caught

The audit is not decoration. It failed twice during development, on bugs that would have
inflated model performance silently:

1. **The cutoff drifted to 12:00 on the three 25-hour DST days.** `Timedelta(days=1)`
   subtracts 24 *absolute* hours, so stepping back a "day" from midnight on a 25-hour day
   lands at 01:00. Fixed by doing the day arithmetic on naive local wall-clock time, where
   a day is a calendar day.
2. **A fixed 24h price lag cannot escape a 25-hour delivery day.** For target
   `2023-10-29 23:00` local, minus 24 UTC hours lands on `2023-10-29 00:00` local — the
   first hour of the *same* auction, which is not published at the cutoff. Three such
   target hours exist in the record and are **dropped**, not fudged.

A third bug was caught by a null check rather than the audit: the weather lookup built its
index with `pd.DatetimeIndex(cutoff.values)`, which drops the timezone and matched **0 of
1,083** cutoffs, filling all 24 weather columns with nulls without raising. `build()` now
asserts that no weather block is entirely null.

All three have regression tests in `tests/test_features.py`.

## 5. Missing data

Ten weather columns have gaps, worst `air_pressure_at_sea_level_se4_24h_mean` at 10.7%.
These are genuine SMHI outages identified in Stage 3, not build errors — every gap is in a
weather column, none in the target or any price feature. They are left as nulls rather than
imputed, because gradient boosting handles missing values natively and imputation would
invent weather that was never observed.

## Deliberate deviation from the schema convention

Every warehouse table so far has a schema class. The feature table does not: its columns
will churn through Stage 5 as features are added and dropped, and a rigid contract would be
friction rather than protection. The guarantees that matter are asserted in code instead —
unique keys, no price-grid gaps, no all-null feature block, and the leakage audit — and
covered by tests.

## 6. Weather: measured, then rebuilt around the answer

The original design gave the model only *observed* weather up to the 11:00 cutoff, on the
grounds that the delivery day's own weather is unknowable. That reasoning had a hole: a
**forecast** for tomorrow, published before the cutoff, genuinely existed at the cutoff.
Using it is not leakage. Refusing it threw away the single most valuable signal available.

`PYTHONPATH=. python models/weather_value_experiment.py` trains one model seven times, changing only what
it may know about the weather. Train 2024-04-01 → 2026-02-19, test the following six months
(4,344 hours). All modes are restricted to the forecast-covered window so they see identical
rows — comparing a forecast model on less data against an observation model on more would
measure sample size, not features.

| What the model may know about weather | Honest? | MAE | RMSE |
|---|---|---|---|
| baseline: yesterday, same hour | — | 25.97 | 35.64 |
| baseline: last week, same hour | — | 33.60 | 44.95 |
| no weather columns at all | yes | 23.22 | 30.48 |
| observed, 24h aggregates at the cutoff | yes | 23.15 | 30.57 |
| observed, persistence | yes | 22.98 | 30.15 |
| **forecast for the target hour, issued d−1** | **yes** | **19.48** | 26.16 |
| **forecast for the target hour, issued d−2** | **yes** | **19.26** | 25.91 |
| the actual observation at the target hour (SMHI) | no — leaks | 19.81 | 26.52 |
| the actual value at the target hour (same provider) | no — leaks | 19.14 | 25.88 |

Differences use a **block bootstrap over whole test days** (182 days, 2,000 resamples);
whole days because neighbouring hours correlate ~0.95.

| Comparison | Δ MAE | 95% CI | Verdict |
|---|---|---|---|
| observed weather vs no weather | +0.07 | [−0.96, +1.14] | not distinguishable from zero |
| **forecast (d−1) vs observed-only** | **+3.67** | **[+2.53, +4.75]** | **real** |
| **forecast (d−2) vs observed-only** | **+3.89** | **[+2.76, +4.98]** | **real** |
| same-source perfect vs forecast (d−1) | +0.35 | [−0.42, +1.13] | not distinguishable from zero |
| same-source perfect vs SMHI perfect | +0.67 | [+0.14, +1.20] | real |
| forecast d−1 vs d−2 | −0.22 | [−0.81, +0.37] | not distinguishable from zero |

### Four conclusions

1. **Observed weather earns nothing.** Dropping all 24 observation columns moves the score
   by an amount whose interval spans zero. It is redundant rather than irrelevant: a 24-hour
   summary is already implied by the price lags, since yesterday's weather is in yesterday's
   prices.
2. **Forecast weather is worth 3.7–3.9 EUR/MWh** — larger than the model's entire advantage
   over the best naive baseline. This is the single biggest improvement found so far.
3. **A day-ahead forecast is statistically as good as knowing the weather exactly.** The gap
   to same-source perfect knowledge is +0.35 with an interval spanning zero. Day-ahead
   forecast error (0.6–1.1 °C for temperature) is simply too small to matter for price.
4. **An apparent absurdity turned out to be a source artefact.** The forecast first appeared
   to *beat* perfect weather. It was beating a worse measurement: Open-Meteo grid values plus
   cloud cover outperform single SMHI station readings by 0.67 (real). Compared within one
   provider, perfect ≥ forecast, as it must be. This is why `lead_days = 0` is stored — so
   the comparison can be made inside a single source.

### Why the shipped table uses the two-day-old forecast

`weather_mode="forecast_d2"` is the default, not `forecast`. The two score identically
(−0.22, CI spans zero), but a forecast issued **two** days before the delivery day is
unambiguously older than the 11:00 cutoff, whereas a one-day-old forecast depends on which
model run produced it — and if that run were issued after 11:00, it would leak. Choosing d−2
costs nothing measurable and removes the question entirely.

The cost is history: forecasts exist from 2024-04-01, so the feature table now spans
**20,900 rows** instead of 25,822 — 29 months instead of 36, still covering two full winters.
Given the effect sizes (+3.9 for forecasts against +0.07 for the extra months of observed
weather), that is a clearly favourable trade.

### Superseded: what this section said first, and why it was wrong

Two earlier versions of this analysis were wrong, and both are worth recording.

**First version** reported MAE 22.14 / 22.59 / 19.81 and concluded weather was worth 2.33.
Those runs were not reproducible: repeats of identical code disagreed by ~0.3 EUR/MWh, the
same magnitude as the differences being measured. Two causes — scikit-learn enables early
stopping above 10,000 rows using a *random* validation split (also wrong for a time series),
and **DuckDB's parallel `avg()` is not bitwise reproducible**, its floating-point summation
order varying between runs so that identical queries returned values differing in their last
bits, which moved gradient-boosting bin edges and changed tree splits. `load_grids()` now
sets `threads=1`. Totals were never affected, only reproducibility.

**Second version**, with those fixed, concluded that a forecast pipeline was *not* worth
building — because it only ever compared observation-based modes and inferred the forecast's
value from a leaky upper bound. Adding real archived forecasts reverses that conclusion
completely: they are worth 3.7, not 0.

Recorded rather than deleted, because the reason a number changed is worth as much as the
number.

## Carried into Stage 5

1. **Split by time, never at random.** Adjacent hours are 0.95 correlated; a random split
   would put near-duplicates on both sides and report a fantasy score.
2. **Baselines first:** yesterday's same hour (`spot_price_se3_lag_24h` used directly) and
   last week's same hour. The lag-24h baseline is the number a model has to beat.
3. **Report MAE alongside RMSE** — the target's right tail reaches €707 and RMSE will be
   dominated by a handful of spikes.
4. **No log transform** — 5.0% of target hours are negative.
5. Weather features are constant within a delivery day; expect feature importance to reflect
   that, and say so plainly when interpreting the model.
