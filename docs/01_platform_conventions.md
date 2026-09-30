# Stage 1 — Platform conventions and project design

**Why this document exists.** This project began with a constraint rather than a dataset: I
was given internal documentation describing a production energy-data platform, but no access
to its systems or its data. The task was not to clone it — it was to learn how that kind of
platform is built and to demonstrate the same way of working on data I could actually obtain.

So before writing any code I extracted the conventions worth mirroring, and recorded
explicitly which parts I adopt as-is, which I substitute with public or local equivalents,
and why. Every substitution below is a deliberate design decision, not a shortcut.

*Out of respect for the organisation that shared its documentation, this file describes
patterns and architecture only. No internal names, schedules, table names or pipeline
inventories appear anywhere in this repository.*

---

## 1. The conventions worth copying

These are code and design conventions rather than infrastructure, so they cost nothing to
adopt and they are what makes a data platform maintainable.

1. **Config-driven ingestion.** Every API endpoint is described in one central config file —
   URL, parameters, time mode, storage target. Adding a source is mostly config, not new
   logic.

2. **Schema classes as contracts.** Every warehouse table has a schema class, and a single
   mapper resolves endpoint name → schema class. Both the manual path and the orchestrated
   path inject the same schema, so field names and types cannot drift apart.

3. **One CLI entrypoint for manual runs.** The same collection code that runs on a schedule
   can be invoked by hand for any date range. Used for debugging and for historical
   backfills.

4. **Partitioned assets with predictable names.** A fetch step, then a save step, then a load
   step — named to a fixed pattern, partitioned by day, so any single day can be re-run in
   isolation.

5. **Asset factories for persistence boilerplate.** A shared factory generates the
   save-and-load asset pair for any endpoint, so nobody writes that code twice.

6. **Jobs and schedules per domain module.** Each source owns its own module, exposing a job
   and a schedule built from its partitions.

7. **Resilience defaults.** Automatic retries with backoff on every fetch; replace-per-
   partition writes so re-running a day is idempotent rather than duplicating.

8. **A single registry.** One file imports every domain module and registers everything that
   runs. If it is not in that file, it does not run.

9. **Secrets from the environment.** Never in code, never in the repository.

10. **ML training data as a wide hourly table.** One row per hour, one column per
    (market, metric, area) combination, with a consistent naming convention and every
    timestamp aligned to a common grain and timezone.

11. **Documentation as part of the work.** Every pipeline has a written page; the checklist
    for adding an endpoint ends with updating it. Even small operational procedures are
    written down.

## 2. Adopted, substituted, and left out

| Production platform | This project | Why |
|---|---|---|
| Dagster orchestration — assets, jobs, schedules, partitions, retries | **Adopted as-is** | The core of the platform, and it runs locally without modification |
| Config-driven endpoints, CLI entrypoint, schema classes and mapper | **Adopted as-is** | Pure code conventions; no infrastructure required |
| Cloud object storage for raw parquet | **Local `data/raw/` parquet** | No cloud access; identical format and partition-by-date layout |
| Cloud data warehouse | **DuckDB, a single local file** | No cloud access; near-identical SQL, runs embedded, free |
| Cloud save-asset factory | **`LocalSaveAsset` factory** | Same factory pattern, different destination |
| Licensed and internal data feeds | **Four public sources** — see [02_data_access.md](02_data_access.md) | Free, documented, and sufficient for the task |
| Warehouse SQL view producing ML training data | **A feature-building step producing the same shape** | Mirrors the data contract using data I can actually obtain |
| Operational database writes and downstream trading flows | **Out of scope** | Requires real trading infrastructure |
| Managed Kubernetes deployment | **Out of scope** — runs under `dagster dev` | Deployment is not the learning goal |
| Event sensors triggering downstream materialization | **Simplified** — the feature build is run explicitly | Adds operational complexity with little value at this scale |

## 3. What this implies for the design

**The prediction task.** The wide hourly training table exists to forecast prices, so the
natural well-scoped task is: *at roughly 11:00 on day D, before the day-ahead auction closes
at 12:00, predict the 24 hourly prices for delivery day D+1 in bidding area SE3.* All four
Swedish bidding areas are collected so that neighbouring-area prices can be used as
predictors and so exploratory analysis can compare them.

**The shape of the feature table.** One row per delivery hour. Columns follow a
`{market}_{metric}_{area}` convention — `spot_price_se3`, `air_temperature_se3_24h_mean` —
with engineered lag, rolling and calendar features appended.

**The leakage rule, and where it comes from.** Every feature must be knowable before the
cutoff. That distinction turns out to be subtler than it first appears: a *forecast* for
tomorrow published before 11:00 genuinely existed at 11:00 and is legitimate, whereas an
*observation* of tomorrow does not exist yet and is not. Getting that distinction right was
the single largest improvement in the project — see
[06_validation.md](06_validation.md).

**Backfill reality.** Some public APIs serve only the most recent day, so historical depth
has to come from separate archive endpoints. The ingestion CLI therefore supports explicit
start and end dates, mirroring the manual-execution pattern.

**Timezone discipline.** Nordic market data crosses daylight-saving boundaries, producing a
23-hour and a 25-hour day every year. Everything is stored in UTC; local time is derived
only where a human question requires it.

## 4. Glossary

- **Bidding area** — a geographic electricity price zone. Sweden has four, SE1 (north) to
  SE4 (south); prices differ because transmission capacity between them is limited.
- **Day-ahead (spot) market** — the daily auction, closing around 12:00, that sets one price
  per hour for the *following* day in each bidding area. These are the prices forecast here.
- **Dagster** — a Python orchestration tool that runs data pipelines on a schedule, retries
  failures, and shows what ran in a web UI.
- **Asset / job / schedule / partition** — Dagster's building blocks: a step that produces a
  dataset; a runnable bundle of steps; a timer that triggers a job; a time slice (here, one
  day) that makes runs repeatable one day at a time.
- **Parquet** — a compressed columnar file format, standard for analytics data.
- **DuckDB** — an analytical SQL database stored in a single local file; the stand-in for a
  cloud warehouse.
- **Data leakage** — letting a model train on information that would not have been available
  at prediction time, producing results that look good and do not survive contact with
  reality.

---
*Next: [02_data_access.md](02_data_access.md) — which public sources were chosen, how they
are collected, and the runbook that populates the warehouse.*
