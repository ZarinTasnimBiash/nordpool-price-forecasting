# Stage 2 — Data Access & Ingestion

## Data source decisions

I use **two** price sources, because neither alone is sufficient.

**Live daily prices: Nord Pool Data Portal public API** (`dataportal-api.nordpoolgroup.com`).
This is the API behind Nord Pool's own public site (data.nordpoolgroup.com). It is free and
needs no account, but it is *not an officially supported API*: it requires browser-like
request headers, has no SLA, and could change without notice. Critically, **it only serves a
rolling ~62-day window** — older dates return 401 (measured 2026-08-19; the edge moved from
T-63 to T-62 within one hour, so the window rolls continuously). Deeper history requires the
paid/partner API. Nord Pool is therefore my *live daily* source only.

**Historical prices: elprisetjustnu.se** (`www.elprisetjustnu.se/api/v1/prices`). A free
Swedish public service republishing the same Nord Pool day-ahead prices, with history back
to 2021 and no account required. One request per (day, area); prices arrive per kWh in local
time and are converted to EUR/MWh in UTC on ingest to match the Nord Pool schema. It is a
community service with no SLA — acceptable because it is used for a one-off historical
backfill, not for live operation.

**The two are cross-checked against each other.** They overlap for the Nord Pool window;
on 19,200 overlapping rows (2026-07-01 → 2026-08-19, all four areas) they agree exactly:
mean and max absolute difference 0.0000 EUR/MWh, correlation 1.00000000. That agreement is
what licenses using elpriset for the ~3 years where no second source is available.

Alternatives considered: the official Nord Pool API (paid/partner access — what a production
uses), and the ENTSO-E Transparency Platform (free with token; carries the same day-ahead
prices plus load and generation — kept as an optional extension for extra features, and the
natural upgrade if elpriset ever becomes unavailable).

**Weather: SMHI Open Data** (`opendata-download-metobs.smhi.se`). Free, no key, official.
Same source and same six parameters as the reference platform's SMHI pipeline
(air temperature, wind direction/speed, relative humidity, 1-hour precipitation,
sea-level air pressure), with one observation station mapped per bidding area:

| Area | Station | Id | Per-parameter override |
|---|---|---|---|
| SE1 | Luleå-Kallax Flygplats | 162860 | — |
| SE2 | Östersund-Frösön Flygplats | 134110 | 1h precipitation → Föllinge A (134410) |
| SE3 | Stockholm-Observatoriekullen A | 98230 | wind speed + direction → Stockholm-Bromma (97200) |
| SE4 | Malmö A | 52350 | sea-level pressure → Malmö-Sturup (53300) |

The overrides exist because no single station measures all six parameters; they were found
by `--validate`, which checks all 24 (area × parameter) combinations. This is also why the
weather table is stored long rather than wide — see `schemas/weather.py`.

SMHI splits each series across two periods, and **both are needed**:
`corrected-archive` (quality-checked, 1996 → ~3 months behind now) and `latest-months`
(the recent tail). They overlap by ~3 weeks, so fetching both gives continuous coverage —
`--mode archive` then `--mode recent`. Fetching only the archive leaves a silent ~3.5-month
hole at the recent end, which is exactly where the prices are freshest.

## How ingestion works (mirrors the reference architecture)

```
config/configs.py         what to fetch (endpoints, areas, stations, storage)
schemas/*                 table contracts + schema_mapper (endpoint -> schema)
ingestion/<source>/       fetch + normalize per source
ingestion/data_handler.py validate against schema -> parquet -> DuckDB
ingestion/main.py         CLI for manual runs & backfills
orchestration/*           Dagster assets/jobs/schedules (daily automation)
```

Key behaviours carried over from the reference conventions: up to 3 retries with backoff on
every fetch; replace-per-partition writes so re-running a day is idempotent; the SMHI
daily asset refuses backfills (the latest-day endpoint only serves the latest day) and
historical depth comes from a separate archive load; Nord Pool partitions use
`end_offset=2` day-ahead offset logic so tomorrow's prices (published ~13:00 CET today)
can be fetched today; schedules staggered (weather 00:30, prices 13:30 local).

Deliberate substitutions (no GCP access): GCS → `data/raw/` parquet;
cloud warehouse → DuckDB (`data/warehouse.duckdb`); cloud save-asset factory → `LocalSaveAsset`;
Kubernetes deployment → `dagster dev` locally. Weather is stored *long*
(one row per time/area/parameter) instead of a wide table, because
per-parameter station overrides make wide rows ambiguous; the wide shape is produced
in the feature stage by pivoting.

## Development constraint worth recording

This project was developed in a cloud sandbox whose network policy blocks all
external data APIs, so live API calls could not be tested during development.
Mitigations: the test suite (`pytest tests/`) runs fully offline against synthetic
payloads; `python -m ingestion.main --validate` performs a live smoke test
(one price fetch + all 24 SMHI station/parameter combos) and is the first command
to run on a real machine; unexpected Nord Pool payload shapes are dumped to
`data/raw/_debug/` for inspection instead of crashing the run.

## Runbook: first-time local setup & backfill (macOS)

```bash
# 1. unzip the project, open a terminal in the project folder, then:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. smoke-test API access and station config (~30 s)
python -m ingestion.main --validate

# 3. backfill ~3 years of historical prices (~4300 requests, ~30 min)
#    if it dies partway, restart from the day after the last file in
#    data/raw/elpriset/elpriset_day_ahead_prices/
python -m ingestion.main --api elpriset \
    --start_date 2023-09-01 --end_date 2026-08-19 --write

# 4. fetch the Nord Pool live window (only ~62 days are available)
python -m ingestion.main --api nordpool --endpoint day_ahead_prices \
    --start_date 2026-07-01 --end_date 2026-08-19 --write

# 5. backfill weather — BOTH periods are required for continuous coverage
python -m ingestion.main --api smhi --mode archive --write   # 1996 .. ~3mo ago
python -m ingestion.main --api smhi --mode recent  --write   # the recent tail

# 6. run the test suite
pytest tests/

# 7. (demo) start the Dagster UI and look at assets/jobs/schedules
dagster dev -f orchestration/definitions.py   # then open http://localhost:3000
```

Everything lands in `data/raw/**.parquet` and `data/warehouse.duckdb`
(both gitignored — data never goes to GitHub, mirroring the reference platform's
code/data separation).

## What actually landed (backfill of 2026-08-19)

| Table | Rows | Span |
|---|---|---|
| `elpriset_day_ahead_prices` | 197,088 | 2023-09-01 → 2026-08-19 (1,084 days, no gaps) |
| `nordpool_day_ahead_prices` | 19,200 | 2026-07-01 → 2026-08-19 (live window) |
| `weather_smhi_data` | 608,550 | 2023-09-01 → 2026-08-19 |

Cross-check on the 19,200 overlapping price rows: mean |diff| 0.0000 EUR/MWh,
max |diff| 0.0000, correlation 1.00000000.

## Known risks / open items

- **Volumes are out of scope (2026-08-19).** The configured `DayAheadVolumes` path
  returns 404 on the live API — it was written from a guess during the no-network
  development period, never verified against a real response. Prices on the same
  base URL work fine, so this is a wrong path, not an access problem. To restore
  volumes: open data.nordpoolgroup.com, F12 → Network → Fetch/XHR, read the real
  URL and payload off the site's own call, then re-add the endpoint to
  `config/configs.py` and correct `normalize_volumes`. The schema class and
  normalizer are still in the repo.
- `--validate` only probes day-ahead prices, so it reported PASSED while the volumes
  endpoint was fictional. Worth extending to probe every configured endpoint.
- SMHI precipitation archives sometimes use a from/to timestamp CSV layout; if the
  archive load reports 0 precipitation rows, this is why (fix: extend the CSV parser).
- **A single failed request aborts a whole backfill.** `fetch_*` raises after 3 retries and
  `collect()` does not catch it, so a transient network drop 20 minutes into a 30-minute run
  ends the run. Nothing already written is lost (per-day parquet + replace-into-DuckDB), so
  the fix is to restart from the last written day — but the reference convention is
  self-healing re-coverage, so making `collect()` skip-and-report would be closer to their
  pattern. Open decision.
- **Nord Pool's rolling window vs. fixed Dagster partitions.** `NORDPOOL_HISTORY_START` is a
  fixed date, but the API's window rolls daily, so the oldest partitions will eventually fall
  outside it and 401 if materialized. Harmless in practice (the daily schedule only ever
  materializes the newest partition) but worth knowing before backfilling old partitions from
  the Dagster UI. A tolerant fix would be to treat 401 like 204 (no data) with a distinct
  warning rather than a hard failure.
- The day-ahead market moved to 15-minute periods in Oct 2025; I store native
  resolution (`resolution_minutes`) and aggregate to hourly in the feature stage.
- DST days have 23/25 hours — all storage is UTC; local-time features are derived
  later with explicit timezone conversion.
