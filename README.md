# Forecasting Tomorrow's Electricity Prices in Sweden

Every day at 12:00, a European auction sets the price of electricity for every hour of the
next day. This project asks a simple question and answers it carefully:

> **At 11:00 today — one hour before the auction closes — how well can I predict tomorrow's
> 24 hourly electricity prices for southern-central Sweden (bidding area SE3)?**

The answer: **a mean absolute error of 19.56 EUR/MWh, against 26.28 for the best simple rule
of thumb.** A 25.6% improvement, and it holds in all ten of the test months.

![Day-ahead price by bidding zone](reports/figures/01_price_history.png)

---

## The result in one table

Ten months of testing (2025-11 → 2026-08). Every month is data the model had never seen when
it was tuned.

| Predictor | Average error (EUR/MWh) | Worst month | Wins |
|---|---|---|---|
| "tomorrow looks like yesterday" (the rule to beat) | 26.28 | 33.7 | — |
| "tomorrow looks like last week" | 34.63 | 41.8 | — |
| **the model** (gradient-boosted trees, 70 features) | **19.56** | **22.2** | **10 / 10 months** |

The improvement is **+6.72 EUR/MWh, 95% CI [+5.13, +8.42]** — measured with a bootstrap that
resamples whole days, not individual hours, because neighbouring hours are ~0.95 correlated
and treating them as independent would manufacture confidence that isn't there.

**The honest footnote:** a plain ridge regression on the same 70 features captures 6.6 of
those 6.7 points. The win belongs to the feature engineering, not to the fancy algorithm.
That is stated here rather than buried, because it is the most useful thing the project
learned.

---

## Why 11:00 matters (the one rule everything obeys)

```
Day D                                          Day D+1
──────────────────────────────────────────►    ─────────────────────►
        11:00        12:00        ~13:00       00:00 ──────── 24:00
          │            │             │          └─ the 24 prices I predict ─┘
          │            │             └─ results published
          │            └─ auction closes
          └─ I make my forecast here
```

Anything a feature uses must have existed **before 11:00 on day D**. That single rule shapes
the entire repository:

- price features are lagged 24 hours or more;
- weather uses **archived forecasts** — what tomorrow was *predicted* to be, published
  a day or two earlier — not what the weather actually turned out to be;
- every feature column is checked by an automated leakage audit before training.

Using forecast weather instead of observed weather is worth **+3.89 EUR/MWh**. It is also the
only honest option: observations for tomorrow do not exist at 11:00 today.

---

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m ingestion.main --validate          # 30s live check: are the APIs reachable?
pytest tests/                                # 47 tests, all offline
dagster dev -f orchestration/definitions.py  # the pipeline UI at localhost:3000
```

**The repository deliberately contains no data.** To fill `data/`, follow the backfill runbook
in [docs/02_data_access.md](docs/02_data_access.md) — about 30 minutes and ~4,300 requests
against free public APIs. No accounts, no API keys, no paid data.

Then, in order:

```bash
python -m ingestion.main --api elpriset --start_date 2021-01-01 --end_date 2026-08-31
python notebooks/03_eda.py                   # regenerates every figure
python features/build_features.py            # builds the 70-feature table
PYTHONPATH=. python -u models/run_stage5.py  # ~50 min: walk-forward validation
PYTHONPATH=. python -u models/run_stage6.py  # error analysis
```

---

## How the project is built

```
   4 public APIs          config-driven          schema classes
 ┌────────────────┐        ingestion              validate every
 │ elprisetjustnu │   ──►  ┌──────────┐   ──►     row before it       ──►  data/raw/*.parquet
 │ Nord Pool      │        │ one CLI  │           is written               + DuckDB warehouse
 │ SMHI           │        │ + Dagster│
 │ Open-Meteo     │        └──────────┘
 └────────────────┘         same code
                            both paths
                                 │
                                 ▼
                     feature builder ──►  20,900 hours × 70 features
                                 │        (leakage-audited)
                                 ▼
                   walk-forward validation ──►  15 monthly folds
                                                5 to tune, 10 to report
```

The command line and the scheduler call **the same collection functions**, so a manual
backfill and a nightly run can never quietly diverge.

---

## Documentation — one document per stage

Each stage has its own write-up with the decisions and the reasoning, including the ones that
turned out to be wrong.

| Stage | What happened | Read |
|---|---|---|
| 1 | Studied the conventions of a production data platform, decided what to copy and what to substitute | [docs/01_platform_conventions.md](docs/01_platform_conventions.md) |
| 2 | Found four usable public data sources, built config-driven ingestion, schemas and the Dagster pipeline | [docs/02_data_access.md](docs/02_data_access.md) |
| 3 | Explored 1.83M rows: data quality, daily and seasonal shape, where weather actually matters | [docs/03_eda.md](docs/03_eda.md) |
| 4 | Built the hourly feature table — lags, rolling windows, calendar, weather — plus the leakage audit | [docs/04_features.md](docs/04_features.md) |
| 5 | Baselines, walk-forward validation, tuning, the model, and prediction intervals | [docs/05_modeling.md](docs/05_modeling.md) |
| 6 | Where the model is wrong and why; three earlier conclusions reversed | [docs/06_validation.md](docs/06_validation.md) |

**Two extra guides, written for different readers:**

- [docs/how_it_works.html](docs/how_it_works.html) — plain language, no code. What each stage
  did, what the real data looked like before and after cleaning, and every bug that had to be
  solved. **Start here if you want the reasoning.**
- [docs/explaining_code_files.md](docs/explaining_code_files.md) — every code file explained
  block by block, with worked examples. **Start here if you want to read the code.**

### Read them in a browser

GitHub shows `.html` files as source code rather than rendering them. The same pages, live:

| Page | What it is |
|---|---|
| [How the forecaster works](https://claude.ai/artifact/NRby2ZZvmEBBBiHYWZzkvM) | the plain-language walkthrough — no code, real data before and after |
| [Explaining the code files](https://claude.ai/artifact/QRkWV2EZN1a3vG8A9ukpXM) | every file, block by block, with a sidebar |
| [SE3 price exploration](https://claude.ai/artifact/ELWGhxxEhoYYR5gXgSh26E) | the exploratory analysis, with every figure |
| [Project roadmap and status](https://claude.ai/artifact/GqycsTPJpirQF9GRbZbqgP) | stage-by-stage status, the pipeline diagram, and remaining work |
| [The talk](https://claude.ai/artifact/Wkt7QDXbmNEasMPAS4yHMS) | the presentation, as a web page |

---

## Data sources

All free, all public, no account required.

| Source | Gives | Note |
|---|---|---|
| [elprisetjustnu.se](https://www.elprisetjustnu.se) | historical day-ahead prices, SE1–SE4 | history back to 2021 |
| [Nord Pool Data Portal](https://data.nordpoolgroup.com) | live daily prices | public API only serves a rolling ~62-day window |
| [SMHI Open Data](https://opendata-download-metobs.smhi.se) | weather **observations** | 6 parameters, one station per bidding area |
| [Open-Meteo previous-runs](https://open-meteo.com) | archived weather **forecasts** | what each hour was predicted to be, 1–2 days earlier; from 2024-04 |

The two price sources overlap, so they were cross-checked on 19,200 hours: mean and maximum
absolute difference **0.0000 EUR/MWh**, correlation **1.0**. That agreement is what justifies
using the historical source for the years the Nord Pool window cannot reach.

---

## Repository layout

```
config/          every source described in one file — URLs, params, stations, storage
schemas/         one class per table; the contract between API payload and warehouse
ingestion/       per-source fetch + normalise, plus the CLI for manual runs and backfills
orchestration/   Dagster assets, jobs and schedules (weather 00:30, prices 13:30)
features/        the feature builder — hourly grid, lags, calendar, weather
models/          baselines, the model, walk-forward validation, error analysis
notebooks/       analysis scripts (03_eda.py regenerates every figure)
tests/           47 tests, all offline against synthetic payloads
docs/            one document per stage
reports/         figures, result JSON, and the presentation
data/            gitignored — raw parquet + the DuckDB warehouse
```

---

## Six decisions worth knowing

1. **Everything is stored in UTC.** Sweden has one 23-hour day and one 25-hour day every year.
   Storing local time would silently corrupt every lag feature on exactly those days.

2. **Prices keep their native resolution** (60-minute before 2025-10-01, 15-minute after) with
   a `resolution_minutes` column, rather than forcing one grain at ingest time and losing the
   ability to tell the difference later.

3. **Weather is stored long**, one row per (time, area, parameter). SE3's wind and SE3's
   temperature come from different stations; a wide table would hide that. The wide shape is
   produced later by pivoting.

4. **Walk-forward validation, not a single split.** The usual 80/20 split does not work on a
   time series. A single chronological holdout was tried first and turned out to be a
   different market — the test window's median price was nearly double the training window's.
   Fifteen expanding-window monthly folds replaced it: five for tuning, ten never touched
   until the final report.

5. **A change is kept only if it survives the noise.** No arbitrary success threshold — any
   number chosen would have been set just under whatever had already been achieved. Instead:
   a block bootstrap over whole days, 2,000 resamples. If the improvement's confidence
   interval spans zero, the change is not kept.

6. **Writes are replace-per-partition**, so re-running any day is idempotent. There is a test
   for it.

---

## What this model is *not* good for

The most useful finding in the project is a bias pattern, not an error number: **the model
shrinks towards the middle.** It predicts too high on cheap hours and too low on expensive
ones.

| True price | Average signed error |
|---|---|
| negative | **+8.18** (predicts too high) |
| 0–20 | **+13.92** |
| 50–100 | −6.81 |
| 200+ | **−97.74** (predicts far too low) |

One mechanism explains three symptoms that had been treated as separate problems: why the
model adds nothing on price spikes, why it adds nothing on negative hours, and why its
prediction intervals leaned. It is **reliable in the ordinary middle of the price
distribution and adds nothing at either extreme.** Anyone who cared mainly about spike hours
would not be well served by it as it stands.

Two earlier explanations for this were tested and killed: tree extrapolation (0 of 7,007 test
hours exceeded their fold's training maximum — the mechanism cannot apply) and regime shift
(correlation only −0.327, with stark counter-examples). Both were removed from the
documentation rather than quietly left in.

### Other known limits

- **No demand-side data.** ENTSO-E load and generation is the largest remaining data gap.
- **Three daylight-saving days** contain overlapping delivery periods that double-cover one
  hour; documented and handled, but worth knowing.
- **A failed request aborts a whole backfill** rather than skipping and reporting it.
- **Nord Pool's public window rolls**, so the oldest Dagster partitions eventually fall
  outside it.
- **The rolling-price features are unresolved** — the folds disagree about whether they help.
  They were kept, and the question is written down rather than answered.

---

## About this project

Built during an internship, as a self-contained exercise: study how a production energy data
platform is organised, then rebuild that shape end to end on public data — config-driven
ingestion, schema contracts, a real Dagster project with partitioned assets, a warehouse, a
feature layer, and an honestly validated model.

Cloud object storage is substituted with local parquet files and the cloud warehouse with
DuckDB; everything else follows the same conventions.
[docs/01_platform_conventions.md](docs/01_platform_conventions.md) records exactly what was
adopted, what was substituted, and why. No internal names, systems, schedules or data from any
organisation appear anywhere in this repository.
