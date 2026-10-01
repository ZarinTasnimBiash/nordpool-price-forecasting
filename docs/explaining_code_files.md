# Explaining code files

Every file in the project, block by block, in plain language — written so I
can read the code myself and explain it to someone else without help.

> Markdown copy of `docs/explaining_code_files.html`, which is the same content
> with syntax highlighting and a sidebar. Regenerate rather than editing both.

Explaining code filesEvery file in the project, block by block, in plain language — written so I can read the code myself and explain it to someone else without help.

## The shape of the project

There are seven layers. Each one has a single job, and each only talks to the layer below it. That separation is the whole point — it's what lets me change where data is stored without touching the code that fetches it.

| Layer | Folder | Its one job |
|---|---|---|
| **1 · Configuration** | `config/` | Describe *what* to fetch. No logic. |
| **2 · Contracts** | `schemas/` | Declare exactly what each table looks like. |
| **3 · Ingestion** | `ingestion/` | Fetch, clean, and store. All the real work. |
| **4 · Orchestration** | `orchestration/` | Run layer 3 automatically, every day. |
| **5 · Features** | `features/` | Turn stored data into one row per hour to predict. |
| **6 · Measuring** | `models/` | Answer "is this feature worth having?" with a number. |
| **7 · Checking** | `tests/`, `notebooks/` | Prove it works; look at what came back. |

An analogy that holds up all the way through: layer 1 is the **shopping list**, layer 2 is the **quality inspector**, layer 3 is the **shopper and the warehouse staff**, layer 4 is the **robot that repeats the trip daily**, layer 5 **prepares the meal**, layer 6 **tastes it**, and layer 7 is the **audit**.

> **Say this**
>
> “The project is layered so each piece has one responsibility. The manual command line and the automatic scheduler both call the identical collection code, so they can never disagree about how data is fetched or stored.”

## How one collection cycle works

Before the file-by-file detail, this section walks through what happens during a single run of the pipeline.

```text
python -m ingestion.main --api elpriset --start_date 2024-02-14 --end_date 2024-02-14 --write
```

1. **main.py** reads the arguments I typed and works out that I want elpriset, one day, and that I mean it (`--write`).
1. **elpriset_data.collect()** loops over the four bidding areas.
1. **fetch_day()** builds a URL and asks the server. It gets back a list of 24 records.
1. **normalize()** turns each record into a tidy row: converts €/kWh into €/MWh, converts local time into UTC.
1. **DataHandler.persist()** hands the rows to the schema, which checks and casts every column.
1. The result is written twice — once as `data/raw/elpriset/.../2024-02-14.parquet`, once into the DuckDB table.
1. I see `collected 96 elpriset price rows (written)`.

96 rows = 24 hours × 4 areas. Whenever a number looks strange, do that multiplication — it catches mistakes fast.

## Layer 1 — the shopping list

**File:** `config/configs.py` — _187 lines · describes all four sources · contains no logic except one small helper_

> **Say this**
>
> “All my sources are described in one config file. Adding a new data source is mostly adding config, not writing new code — that's a convention taken directly from the reference platform's platform.”

### Block 1 — finding the project folder

```python
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
```

`__file__` means “the location of this very file”. `.parent` goes up one folder. Two `.parent`s take me from `config/configs.py` up to the project root.

**Why not just write the path?** Because a hard-coded `/Users/zarin/...` breaks on every other computer. This works out the answer freshly wherever it runs.

### Block 2 — the two history dates

```text
HISTORY_START = "2023-09-01"
NORDPOOL_HISTORY_START = "2026-07-01"
```

This is the most important pair of lines in the file, and it exists because of something I discovered by testing.

- `HISTORY_START` — how far back the *historical* sources go (elpriset prices, SMHI weather archive). Three years.
- `NORDPOOL_HISTORY_START` — Nord Pool's free API only serves roughly the **last 62 days**. Ask for anything older and it refuses with a `401 Unauthorized`.

> **Worth understanding:** that 62-day window *rolls* — I watched its edge move by a day while working. So this date is deliberately set well *inside* the window rather than at its edge, to leave margin.

### Block 3 — where data goes

```text
STORAGE = {
    "raw_dir": PROJECT_ROOT / "data" / "raw",
    "duckdb_path": PROJECT_ROOT / "data" / "warehouse.duckdb",
}
```

The `/` is *not division* — `pathlib` reuses it to mean “join folder names”, which reads almost like a real path and handles Mac/Windows slashes for me.

Two destinations, mirroring the reference platform's two: raw untouched files (their Google Cloud Storage) and a queryable database (their BigQuery). Keeping the raw files matters — if I later find a bug in my cleaning code, the originals are still there to re-process.

### Block 4 — `api_config`, the nested description of every source

One dictionary holding four sub-dictionaries: `nordpool`, `elpriset`, `openmeteo`, `smhi`. They have *different shapes*, because each source works differently — Nord Pool has several endpoints, SMHI has stations and parameters.

#### The browser disguise

```text
"headers": {"User-Agent": "Mozilla/5.0 ...", "Origin": "https://data.nordpoolgroup.com", ...}
```

Headers are labels attached to a web request. Nord Pool's API is built for their own website and rejects anything that doesn't look like a browser. These lines say “I'm Chrome on a Mac, and I came from your site.” Without them I get `401` on every call.

#### Politeness

```text
"rate_limit_seconds": 0.3
```

Wait 0.3s between calls. A backfill makes thousands of requests; firing them as fast as possible looks like an attack and gets me blocked.

### Block 5 — `get_station()`, the only logic in the file

```python
def get_station(area, parameter_name):
    cfg = api_config["smhi"]
    return cfg["station_overrides"].get(area, {}).get(
        parameter_name, cfg["stations"][area]
    )
```

**The problem it solves:** no single weather station measures everything. Stockholm's station stopped reporting wind. So `stations` is the default answer and `station_overrides` is the exceptions list.

The `get_station()` function determines which SMHI weather station should be used for a given electricity bidding area and weather parameter. Each area has a default station—for example, SE3 normally uses station `98230` in Stockholm for measurements such as temperature, humidity, and air pressure. However, if the default station does not provide a particular measurement, the configuration can define an exception. In SE3, wind data is taken from station `97200` instead. The function therefore first checks whether a special station has been defined for the requested parameter; if so, it uses that station, otherwise it falls back to the area's default station.

The key is `.get(key, fallback)` — “give me this, or this fallback if it's missing”. Unlike `[key]`, which crashes when the key isn't there.

> **Worked example**
>
> `get_station("SE3", "wind_speed")`
> ```text
> 1. cfg["station_overrides"].get("SE3", {})
>    → {"wind_from_direction": 97200, "wind_speed": 97200}
>
> 2. .get("wind_speed", 98230)
>    → 97200   # found in the exceptions
> ```
> `get_station("SE3", "air_temperature")`
> ```python
> 1. same exceptions dict for SE3
> 2. .get("air_temperature", 98230)
>    → 98230   # not an exception, so the default
> ```
> The empty `{}` in step 1 is the trick: areas with *no* exceptions flow through the same two lines without a single `if`.

> **Know this trap:** three keys in this file — `table_name`, `time_mode`, and the SMHI `endpoint` — are never read by any code. The real table name comes from the schema classes. They look like configuration but are currently just documentation.

## Layer 2 — the contracts

**File:** `schemas/base_model.py` — _67 lines · the rulebook every table schema inherits_

> **Say this**
>
> “Every warehouse table has a Python schema class that defines its columns and types. The same schema is injected into both the manual path and the Dagster path, so the two can't drift apart. That's the reference platform's contract pattern.”

Think of this as a **quality inspector with a clipboard**. The clipboard says exactly which columns must exist, in which order, holding which kind of value. Every batch of data gets re-packed to match before it's allowed into storage.

### Block 1 — what a subclass must declare

```text
class BaseSchema:
    columns: dict[str, str] = {}   # ordered {column name: type}
    _table_name: str = ""          # the table's name in DuckDB
    timestamp_field: str = ""      # which column is "the time"
```

`timestamp_field` matters more than it looks: it's the column used to decide which rows to replace when I re-run a day. More on that in [data_handler](#data_handler).

### Block 2 — `validate()`, the heart of the file

```python
for col, dtype in cls.columns.items():
    if col not in out.columns:
        out[col] = pd.Series([None] * len(out))   # missing → nulls
    if dtype.startswith("datetime64"):
        out[col] = pd.to_datetime(out[col], utc=True).astype(dtype)
    else:
        out[col] = out[col].astype(dtype)
return out[list(cls.columns)]   # drop extras + fix the order
```

Three things happen, in order: **add** any column the data forgot (as nulls), **cast** every column to its declared type, and **drop** anything not on the list while putting the rest in the declared order.

> **Worked example**
>
> Suppose a normalizer accidentally hands over this:
> ```text
> {"delivery_area": "SE3", "price_eur_mwh": "27.9", "surprise_field": 1}
> ```
> After `validate()`:
> ```text
> → surprise_field removed (not on the clipboard)
> → price_eur_mwh "27.9" (text) becomes 27.9 (number)
> → delivery_start_utc added, filled with null
> → columns come out in the declared order, every time
> ```

> **The double edge:** this makes the pipeline robust — a slightly wrong payload won't crash it. It also makes failure *quiet*: if a source renames a field, I get a column of nulls rather than an error. That's why I check row counts after a backfill instead of trusting that it finished.

### Block 3 — `duckdb_schema()`

```text
_DUCKDB_TYPES = {"datetime64[ns, UTC]": "TIMESTAMPTZ", "float64": "DOUBLE", ...}
```

Pandas and DuckDB use different names for the same idea. This dictionary translates, so the class can generate the `CREATE TABLE` line automatically. The reference platform's version produces BigQuery types instead — same method, different destination.

## The three label cards

**File:** `schemas/nordpool.py · schemas/weather.py · schemas/elpriset.py · schemas/openmeteo.py` — _one class per warehouse table · pure declarations, no logic_

Each is just a list of columns and their types. What's worth understanding is *why* the columns are what they are.

### `NordpoolDayAheadPrices` — and why each column earns its place

| Column | Why it exists |
|---|---|
| `delivery_start_utc` | Together with the area, this identifies a row uniquely. It's what makes re-running a day safe. |
| `delivery_area` | SE1–SE4. One price per area per hour. |
| `price_eur_mwh` | The number being forecast. |
| `resolution_minutes` | 60 before Oct 2025, 15 after. Storing this instead of assuming one is why the market's change didn't corrupt the data. |
| `delivery_date_cet` | “Which auction was this?” is a different question from “which hour is this?”. The 23:00 UTC row belongs to the *next* Swedish day's auction. |
| `updated_at_utc` | Nord Pool sometimes republishes corrected prices. This says which version I hold. |

> **Why everything is stored in UTC.** Sweden changes clocks twice a year, so one day each year has 23 hours and another has 25. Store local time and those days get duplicate or missing hours, and my lag features quietly break. UTC has no such days. I convert to local time later, deliberately.

### `WeatherObservation` — the long-vs-wide decision

Weather is stored **long**: one row per *(time, area, parameter)*. So one hour in SE3 produces six rows, not one row with six columns.

**Why?** Because of the station overrides. SE3's wind comes from Bromma while SE3's temperature comes from Observatoriekullen. A single wide row labelled “SE3 at 14:00” would imply all six numbers came from one place. Long format keeps `station_id` attached to *every value*, so nothing is hidden.

The wide, model-ready shape gets built later by pivoting — which is exactly how the reference platform does it too (shape the data in the warehouse layer, not at ingest).

> **One class in here is dead code.** `NordpoolDayAheadVolumes` is still defined and still in the mapper, but its endpoint was removed after the live API returned 404 for the configured path — it had been written from a guess, never from a real response. Kept because it is the first thing to restore once the real URL is captured from the Data Portal's own network calls.

### `ElprisetDayAheadPrices` — a separate table on purpose

It could have shared Nord Pool's table with a `source` column. It doesn't, for two reasons: the sources overlap for ~62 days so keeping them apart lets me *compare them*, and the reference platform's convention is one schema class per table.

It also keeps `price_sek_kwh` and `exchange_rate` — free in the payload, and the first place to look if the two sources ever disagree on euros.

### `WeatherForecast` — the table with an extra dimension

Stored long like the observations, but with one column that carries the whole point: `lead_days`. 0 is what the hour actually turned out to be, 1 and 2 are what it was forecast to be one or two days earlier.

> **lead 0 is stored but never used as a feature.** It is the actual outcome, so feeding it to the model would be leakage. It exists so that forecast error can be measured inside a single provider — which is what caught a result that briefly looked impossible: the forecast appearing to beat perfect weather. It was beating a worse *measurement* of the weather, not the weather itself.

> **Say this**
>
> “I cross-checked the two price sources on 19,200 overlapping hours. They agree to 0.0000 EUR/MWh with correlation 1.0. That's what justifies trusting the historical source for the three years the other one can't cover.”

## The lookup table

**File:** `schemas/utils.py` — _19 lines · maps an endpoint name to its schema class_

```text
schema_mapper = {
    "day_ahead_prices":          NordpoolDayAheadPrices,
    "day_ahead_volumes":         NordpoolDayAheadVolumes,
    "elpriset_day_ahead_prices": ElprisetDayAheadPrices,
    "weather_observations":      WeatherObservation,
    "weather_forecast":          WeatherForecast,
}
```

A tiny file doing something important. Notice the values are **classes, not instances** — there are no brackets after the names. I'm storing the blueprint itself, and looking it up by string.

**Why it matters:** both the CLI path and the Dagster path resolve their schema through this one dictionary. There is exactly one place that decides which contract applies to which endpoint, so the two paths physically cannot drift apart.

## Layer 3 — the warehouse worker

**File:** `ingestion/data_handler.py` — _87 lines · validate → parquet → DuckDB_

> **Say this**
>
> “Writes are replace-per-partition, so re-running any day is idempotent — you get the same result whether you run it once or five times. That mirrors the reference platform's save-asset behaviour.”

### Block 1 — it's told its schema, it doesn't choose one

```python
def __init__(self, schema, api, endpoint):
    self.schema = schema
```

The handler receives the schema from outside rather than looking it up. That's called *dependency injection*, and it's why the same class serves prices and weather without a single `if`.

### Block 2 — `load_duckdb()`, where idempotency lives

```text
con.execute(f'DELETE FROM {table} WHERE "{ts}" >= ? AND "{ts}" <= ?', [tmin, tmax])
con.execute(f"INSERT INTO {table} SELECT * FROM incoming")
```

Read it as: **delete everything in the time range I'm about to write, then write it.** Not “add these rows” — “make this time range look like this”.

> **Why that matters**
>
> ```text
> append semantics:  run a day 3 times → 288 rows (wrong, tripled)
> replace semantics: run a day 3 times → 96 rows (correct)
> ```
> This is tested in `test_data_handler_roundtrip`, which persists the same batch twice and asserts the count stays at 8.

The `?` placeholders are **parameterised queries** — values are passed separately rather than glued into the SQL string. That's the standard defence against SQL injection and it also handles date formatting correctly.

### Block 3 — `persist()`, the whole path in five lines

```python
df = self.to_dataframe(records)   # validate against the schema
if df.empty: return df
self.save_parquet(df, partition_label)
self.load_duckdb(df)
```

Raw records in, validated table rows out, written to both destinations. Every source in the project ends its journey here.

## The Nord Pool shopper

**File:** `ingestion/nordpool/nordpool_data.py` — _167 lines · live daily prices (rolling ~62-day window only)_

### Block 1 — `fetch_endpoint()` and the retry loop

```python
for attempt in range(1, max_retries + 1):
    try:
        r = requests.get(url, params=params, headers=CFG["headers"], timeout=30)
        if r.status_code == 204:
            return None   # nothing published yet — not an error
        r.raise_for_status()
        return r.json()
    except Exception as e:
        wait = 2**attempt   # 2s, then 4s, then 8s
        time.sleep(wait)
```

**Exponential backoff:** each retry waits twice as long. If a server is briefly overloaded, hammering it every 0.1s makes things worse; backing off gives it room to recover.

**The 204 line is a real distinction.** “No content” means the auction hasn't published yet — a normal state, not a failure. It returns `None` and the caller skips that day quietly.

### Block 2 — `normalize_prices()`, unpacking the nesting

The API returns data nested two levels deep. One entry covers one time period and contains all four areas inside it:

```text
{"deliveryStart": "2025-06-09T22:00:00Z",
 "deliveryEnd": "2025-06-09T23:00:00Z",
 "entryPerArea": {"SE1": 10.5, "SE2": 10.5, "SE3": 27.9, "SE4": 41.02}}
```

The normalizer flattens that into **one row per area**:

```python
for entry in payload.get("multiAreaEntries", []):
    for area, price in (entry.get("entryPerArea") or {}).items():
        rows.append({...})
```

> **The arithmetic to remember**
>
> ```text
> 1 entry × 4 areas → 4 rows
> 96 entries × 4 areas → 384 rows   # one day at 15-minute resolution
> ```

### Block 3 — `_dump_debug()`, failing loudly

```text
p.write_text(json.dumps(payload, indent=2, default=str))
logger.error("normalize failed ... raw payload dumped to %s", p)
```

If a payload has an unexpected shape, the raw JSON is saved to `data/raw/_debug/` instead of the run crashing. I get the actual evidence to look at.

> **An honest limitation.** This only catches shapes that raise an error. If the source renamed `multiAreaEntries`, `payload.get("multiAreaEntries", [])` would politely return an empty list, no error would be raised, and I'd get `collected 0 rows` with a successful exit. Loud failures are handled; silent ones need me to sanity-check the counts.

## The history shopper

**File:** `ingestion/elpriset/elpriset_data.py` — _113 lines · three years of historical prices_

> **Say this**
>
> “The free Nord Pool API only serves about 62 days, so it can't provide training history. I added a second source for history and kept Nord Pool as the live daily source, then cross-checked them on the overlap.”

### Block 1 — building the URL from a date

```text
d = date.fromisoformat(day)
url = f"{CFG['base_url']}/{d.year}/{d.month:02d}-{d.day:02d}_{area}.json"
```

`:02d` means “pad to 2 digits with a zero”. February is `02`, not `2`. Get that wrong and every request 404s.

Note this source needs **one request per area**, where Nord Pool returns all four at once. That's why the backfill was ~4,300 requests and took half an hour.

### Block 2 — `normalize()` does two conversions

```text
start = pd.Timestamp(rec["time_start"]).tz_convert("UTC")
...
"price_eur_mwh": rec["EUR_per_kWh"] * 1000.0,
```

This source speaks a different dialect, so the differences are resolved *at the door*:

|  | Arrives as | Stored as |
|---|---|---|
| Price | `0.04493` €/kWh | `44.93` €/MWh |
| Time | `2024-02-14T00:00:00+01:00` | `2024-02-13 23:00:00+00:00` |

The `+01:00` is what makes the time conversion safe — the source tells me its offset, so `tz_convert` is exact, including on the days the clocks change.

> **Proof it handles the awkward day**
>
> ```text
> 2024-03-31 is a 23-hour day in Sweden (clocks spring forward)
> elpriset_data.collect("2024-03-31", "2024-03-31")
> → SE3 produced 23 rows, not 24 ✓
> ```

### Block 3 — `collect()` counts what's missing

```python
if payload:
    day_rows.extend(normalize(payload, area))
else:
    missing += 1
```

A `404` here means “no prices for that day and area”, which is different from a broken request. The loop counts these and warns at the end, so a quiet gap becomes a visible number.

## The forecast shopper

**File:** `ingestion/openmeteo/openmeteo_data.py` — _137 lines · what the weather was PREDICTED to be, not what it did_

> **Say this**
>
> “SMHI tells me what the weather actually did, which is only knowable afterwards. At the 11:00 cutoff I am forecasting tomorrow, and tomorrow's observations do not exist yet. A forecast published *before* the cutoff genuinely was available, so using it is not leakage — and it turned out to be worth 3.7 EUR/MWh.”

### Why this file exists at all

The original design excluded every kind of "tomorrow weather" as leakage. That was too broad, and it cost the model its largest single signal. Only tomorrow's **observations** are unavailable at 11:00. Tomorrow's **forecast** is a fact about today.

This module fetches an archive of those past forecasts, keyed by how old each forecast was.

### Block 1 — the lead-time dimension

```text
lead_days = 0   what the hour actually turned out to be
lead_days = 1   what it was forecast to be, one day earlier
lead_days = 2   what it was forecast to be, two days earlier
```

That extra column is the whole point of the table. Only `lead_days >= 1` is ever used as a feature.

> **Why keep lead 0 at all, if it leaks?** So forecast error can be measured *within one provider*. Comparing an Open-Meteo forecast against an SMHI station reading would mix up forecast error with the difference between a grid cell and a weather mast — and that confusion nearly produced a nonsense result (see below).

### Block 2 — one request per area, not per day

```python
fields = []
for var in CFG["variables"]:
    fields.append(var)                                # lead 0
    for lead in CFG["lead_days"]:
        fields.append(f"{var}_previous_day{lead}")
```

One request covers a whole date range and every variable at every lead time, so the backfill was about twenty calls rather than thousands — unlike the price source, which needs one call per day and area.

### Block 3 — a bug caught before the backfill, not after

```python
# Loop dates on the OUTSIDE and areas on the inside, so one write covers
# every area for a time range.
while cursor <= stop:
    for area in areas:
        chunk_rows.extend(normalize(fetch_range(area, ...), area))
    handler.persist(chunk_rows, label)
```

> **Why the loops are that way round.** `DataHandler.load_duckdb` replaces rows by *timestamp range alone*. Writing one area at a time would have deleted the areas written before it, leaving only the last one. Looping dates outside and areas inside means each write covers every area for that range. Spotted by reading the persistence code before running a million-row backfill, rather than by wondering afterwards why three zones were missing.

### What the numbers say

| Weather the model may see | Honest? | MAE |
|---|---|---|
| none at all | yes | 23.22 |
| observed, summarised at the cutoff | yes | 23.15 |
| **forecast for the delivery hour** | **yes** | **19.26** |
| the actual outcome at that hour | no — leaks | 19.14 |

A day-ahead forecast is statistically indistinguishable from knowing the weather exactly: the gap is +0.35 with a confidence interval spanning zero. Forecast error of 0.6–1.1 °C is simply too small to matter for price.

## The weather shopper

**File:** `ingestion/smhi/smhi_data.py` — _165 lines · three collection modes, JSON and CSV_

### Block 1 — three modes, because SMHI splits its data

| Mode | SMHI period | Covers | Used by |
|---|---|---|---|
| `latest` | latest-day | most recent day only | the daily Dagster job |
| `recent` | latest-months | ~last 4 months | backfill (the tail) |
| `archive` | corrected-archive | 1996 → ~3 months ago | backfill (the bulk) |

> **This cost me a real bug.** The quality-checked archive lags about 3.5 months behind today. Originally I fetched only that, which left a silent hole at the recent end — exactly where the prices were freshest. The two periods overlap by ~3 weeks, so running both gives continuous coverage.

### Block 2 — parsing a CSV that doesn't start where I'd expect

```python
header_idx = next(
    (i for i, ln in enumerate(lines) if ln.startswith("Datum;Tid (UTC)")), None
)
```

SMHI's CSV files begin with several lines of station metadata before the real table starts. This scans for the actual header line and starts reading from there.

`next(...)` with a generator means “give me the first match and stop looking” — it doesn't scan the whole file. The `None` at the end is the answer if nothing matches, which is then handled rather than crashing.

Also note `sep=";"` — Swedish CSVs are semicolon-separated, because the comma is their decimal point.

### Block 3 — the nested loop

```python
for area in BIDDING_AREAS:                              # 4
    for param_name, param_id in CFG["parameters"].items():   # 6
        station = get_station(area, param_name)
```

4 areas × 6 parameters = **24 downloads** per run. That 24 is the number I quote throughout the project.

### Block 4 — `validate_stations()`

Checks all 24 combinations exist and are active, returning a list of problems (empty list = all good). **This is the function that found my three station overrides** — it reported that Stockholm had no wind sensor, Frösön no rain gauge, and Malmö A no pressure sensor.

Returning a *list of problems* rather than raising on the first one is deliberate: I learn about all 24 in one run instead of fixing them one at a time.

## The front door

**File:** `ingestion/main.py` — _112 lines · the command line I type at_

> **Say this**
>
> “There's one CLI entrypoint for manual runs and backfills, and it calls exactly the same collection functions that Dagster calls on a schedule. That's the reference platform's manual-execution pattern.”

### Block 1 — argparse is a form

```text
ap.add_argument("--api", choices=["nordpool", "elpriset", "smhi"])
ap.add_argument("--write", action="store_true")
```

Two kinds of field. `--api nordpool` takes a *value*; `--write` is a *switch* — present means true, absent means false.

`choices=[...]` means a typo like `--api nordpol` is rejected instantly, rather than failing halfway through a long run.

> **`--write` is the safety catch.** Without it everything downloads and processes but nothing is saved — a dry run. Always dry-run a new command once before letting it touch my data.

### Block 2 — rules argparse can't express

```python
if not (args.start_date and args.end_date):
    ap.error("nordpool requires --start_date and --end_date")
```

Dates are required for prices but meaningless for SMHI, so the rule depends on another answer. That can't be declared in `add_argument` — it has to be checked by hand.

### Block 3 — `run_validate()`, the 30-second health check

Probes the price API for one day, prints a *real normalized row* so I can eyeball it, then checks all 24 station combinations. Run it before any long backfill.

> **Two known gaps, worth admitting if asked.** It only probes prices, not every configured endpoint — which is how a completely fictional endpoint once passed validation. And if the probe returns “no data”, it reports `OK — 0 delivery periods` and still passes.

### Block 4 — the two lines everyone asks about

```python
if __name__ == "__main__":
    sys.exit(main())
```

Python sets `__name__` automatically. If I *run* this file it becomes `"__main__"` and the block executes. If something *imports* the file it becomes `"ingestion.main"` and the block is skipped.

Without this guard, merely importing the file would kick off a download. And `sys.exit(main())` passes the return value out as an **exit code**: `0` means success, anything else means failure — which is what lets me chain commands with `&&`.

## Layer 4 — the robot

**File:** `orchestration/utils/local_save_asset.py` — _49 lines · a function that builds two Dagster assets_

First, vocabulary. In Dagster, an **asset** is a thing that gets produced — a file, a table. I write a function that produces it and mark it `@asset`. Dagster then knows how to run it, retry it, and show it on a screen.

### The idea: a function that writes functions

```python
def build_save_assets(endpoint, api, suffix, partitions_def, group_name):
    handler = DataHandler(schema_mapper[endpoint], api, endpoint)

    @asset(name=f"save_{endpoint}_to_parquet_{suffix}", ...)
    def save_to_parquet(context, df): ...

    @asset(name=f"save_{endpoint}_to_duckdb_{suffix}", ...)
    def save_to_duckdb(context, df): ...

    return [save_to_parquet, save_to_duckdb]
```

Saving to parquet and loading to DuckDB is identical for every source — only the names differ. Rather than copy that code per source, this **factory** stamps out the pair on demand.

> **What one call produces**
>
> ```text
> build_save_assets("day_ahead_prices", "nordpool", "daily", ...)
> → save_day_ahead_prices_to_parquet_daily
> → save_day_ahead_prices_to_duckdb_daily
> ```

**How the chain is declared:** `ins={"df": AssetIn(upstream)}` tells Dagster “this asset needs that asset's output first”. The dependency graph I see in the UI is built from these declarations — I never write the ordering myself.

The reference platform's version is called `CloudSaveAsset` and writes to Google Cloud Storage and BigQuery. Mine is `LocalSaveAsset` and writes to a folder and DuckDB. **Same pattern, different destination** — that's the substitution recorded in the Stage 1 analysis.

## The daily price job

**File:** `orchestration/nordpool_assets.py` — _60 lines · partitions, the asset chain, a job and a schedule_

### Block 1 — partitions, or: a wall of mailboxes

```text
daily_partitions = DailyPartitionsDefinition(
    start_date=NORDPOOL_HISTORY_START, end_offset=2, timezone="Europe/Stockholm",
)
```

A **partition** is one labelled box for one day. Picture a wall of them, one per date. Three reasons this is worth doing:

- I can refill *one* box without touching the others.
- The UI shows the wall as a grid, so gaps are visible at a glance.
- Refilling replaces rather than duplicates (that's [data_handler](#data_handler) again).

#### `end_offset=2` — the bug I found and fixed

`end_offset` means “how many days into the future should have a box”.

> **Measured, not guessed**
>
> ```text
> simulating the 13:30 tick on 2026-08-19:
>
> end_offset=0 → newest box is 2026-08-18
> end_offset=1 → newest box is 2026-08-19   # what it was — a day behind
> end_offset=2 → newest box is 2026-08-20   # what I want
> ```
> At 13:30 the auction has just published *tomorrow's* prices. With `end_offset=1` the job asked for *today's* — which had been published 24 hours earlier. It was permanently one day behind. Changing one character fixed it.

### Block 2 — the factory-inside-a-loop pattern

```python
for _endpoint in ENDPOINTS:
    def _make_get(endpoint):
        @asset(name=f"get_{endpoint}_daily", ...)
        def get_endpoint_daily(context):
            day = context.partition_key
            return nordpool_data.collect(endpoint, day, day, write=False)
        return get_endpoint_daily
    assets.append(_make_get(_endpoint))
```

> **Why the extra wrapper function?** This is a classic Python trap. If I define a function inside a loop and it refers to the loop variable directly, all the created functions end up sharing the *final* value of that variable. Passing it into `_make_get(endpoint)` gives each one its own private copy. It looks like pointless indirection; it is not.

Notice `write=False` in the fetch asset. The fetching asset only *fetches*; the saving assets save. One asset, one job.

And `context.partition_key` is how the asset learns which box it's filling — Dagster passes the date in.

### Block 3 — job and schedule

```python
nordpool_job = define_asset_job("nordpool_job", selection=[a.key for a in assets])
nordpool_schedule = build_schedule_from_partitioned_job(
    nordpool_job, hour_of_day=13, minute_of_hour=30)
```

A **job** is “run these assets together”. A **schedule** is a timer that starts a job. 13:30 local, because the auction publishes around 13:00.

## The daily weather job

**File:** `orchestration/SMHI_assets.py` — _60 lines · same shape, plus one guard_

### The interesting part — a job that refuses to backfill

```python
latest = daily_partitions.get_last_partition_key()
if context.partition_key != latest:
    raise Failure(
        f"SMHI latest-day endpoint cannot backfill: ... Use "
        f"`python -m ingestion.main --api smhi --mode archive --write`."
    )
```

The SMHI latest-day endpoint *always* returns the most recent day, whatever date I ask for. So filling an old box would silently write today's weather under yesterday's label — data that looks fine and is wrong.

Rather than allow that, the asset refuses, **and the error message names the command that does work.** That last part is the bit worth copying: an error that tells me the fix is worth ten that just say no.

Schedules are staggered — weather at 00:30, prices at 13:30 — which is the reference platform's convention too. It spreads load and makes logs easier to read.

## The registry

**File:** `orchestration/definitions.py` — _17 lines · the single source of truth for what runs_

```text
assets = [*nordpool_assets.assets, *SMHI_assets.assets]
jobs = [*nordpool_assets.jobs, *SMHI_assets.jobs]
schedules = [*nordpool_assets.schedules, *SMHI_assets.schedules]
defs = Definitions(assets=assets, jobs=jobs, schedules=schedules)
```

The `*` is the **splat operator**: it unpacks a list into the one being built. `[*[1,2], *[3]]` gives `[1,2,3]`, not `[[1,2],[3]]`.

If something isn't registered here, Dagster doesn't know it exists. One file answers “what does this system run?” — which is exactly why the reference platform keeps a single `definitions.py` too.

**Note what is *not* here.** Dagster schedules Nord Pool and SMHI only. The historical price source and the forecast archive are driven by the CLI, because both are backfills rather than daily feeds — promoting them to scheduled assets is listed as remaining work.

> **What's registered right now**
>
> ```text
> 6 assets:
> get_day_ahead_prices_daily
> save_day_ahead_prices_to_parquet_daily
> save_day_ahead_prices_to_duckdb_daily
> get_weather_observations_daily
> save_weather_observations_to_parquet_daily
> save_weather_observations_to_duckdb_daily
>
> 2 schedules: nordpool_job_schedule (13:30), SMHI_job_schedule (00:30)
> ```

## Layer 5 — turning stored data into model food

**File:** `features/build_features.py` — _380 lines · one row per hour to predict · 20,900 rows x 70 features_

> **Say this**
>
> “Each row is one question and its answer: given everything knowable at 11:00 the day before, what was the price at this hour? An automated audit refuses to build the table if any feature reaches past that cutoff.”

### Block 1 — the hourly grid, and the rule that fixes the DST defect

```text
-- spread each delivery period across the hours it touches, then let the
-- SHORTER (more specific) period win any hour claimed twice
min(resolution_minutes) over (partition by hour_utc, area)
```

Prices arrive as 15-, 60- and (on clock-change days) 120-minute blocks. Quarter-hours are averaged into their hour. Where a 2-hour block and a 1-hour block both cover 01:00, the 1-hour price wins because it is more specific.

### Block 2 — the timing rule, which is not one rule but two

| Source | Available at the 11:00 cutoff | Safe rule |
|---|---|---|
| Prices | all of day D — published ~13:00 on day D−1 | lag >= 24h |
| Weather observations | only up to D 11:00 | aggregate over a window *ending at* the cutoff |
| Weather forecasts | the delivery day itself, if issued before the cutoff | use directly — not leakage |

A 24-hour lag is safe for prices because the whole of day D was published a day early. The same lag on an *observation* would leak by up to seven hours.

### Block 3 — the audit, which failed twice and was right both times

```text
target                 : Mon 30 Oct 00:00
1. midnight that day   : Mon 30 Oct 00:00
2. minus "one day"     : Sun 29 Oct 01:00   # should be 00:00
3. add 11 hours        : Sun 29 Oct 12:00   # WRONG, an hour late
```

> **Two bugs, one awkward day.** Sweden's clocks go back once a year, making a 25-hour day. `Timedelta(days=1)` subtracts 24 *elapsed* hours, which on that day lands at 01:00 rather than midnight — drifting the cutoff to 12:00 and handing the model an extra hour. Separately, a fixed 24-hour lag cannot escape a 25-hour delivery day: for its last hour it lands on the *first* hour of the same day, a price from the same unpublished auction. The cutoff now uses calendar arithmetic; those three target hours are dropped.

### Block 4 — a line that looks like nothing

```text
con.execute("set threads=1")
```

> **This one is worth understanding.** DuckDB's `avg()` is a parallel aggregation, and its floating-point summation order is not fixed between runs. Identical queries returned values differing in their last bits — which moved gradient-boosting bin edges, changed tree splits, and shifted model MAE by ~0.08 run to run. That is the same size as the effects being measured, so an entire experiment was reading noise and produced the wrong conclusion. Totals were never affected; only reproducibility. Single-threaded, fits are now identical across processes.

### Block 5 — the two columns that get thrown away

`hour_utc` and `cutoff_utc` are carried through the table and then dropped before training. They are the date on an exam paper: used to file the papers in order and to check nobody answered early, never to grade the student.

> **Why a raw timestamp is poison**
>
> Every test timestamp is larger than every training timestamp. A tree splitting on it puts all 4,344 test rows in the same final bucket and predicts one number for all of them — memorising *when* instead of learning *why*. The useful parts (hour, weekday, month) are already extracted as calendar features, which do generalise.

## Layer 6 — is this feature actually worth having?

**File:** `models/weather_value_experiment.py` — _161 lines · trains the same model seven times, changing only the weather_

> **Say this**
>
> “Before building anything to improve the weather features, I measured what they were worth. The answer reversed a decision I had already written down — twice.”

Seven modes, identical in every respect except what the model may know about the weather: `none`, `cutoff`, `persistence`, `perfect`, `perfect_om`, `forecast`, `forecast_d2`. Two of them leak on purpose, to measure a ceiling that a model is not allowed to reach.

### The block bootstrap, and why it is by day

```python
picks = [concatenate([by_day[d] for d in rng.choice(uniq, len(uniq), True)])
         for _ in range(N_BOOT)]
```

Whole days are resampled, not individual hours. Neighbouring hours correlate about 0.95, so resampling hours independently would pretend there is far more evidence than there is, and every difference would look significant.

> **What it reports**
>
> ```text
> observed weather vs no weather   +0.07  95% CI [-0.96, +1.14]  not distinguishable
> forecast (d-2) vs observed-only  +3.89  95% CI [+2.76, +4.98]  REAL
> ```
> A difference is only claimed when the interval excludes zero. That discipline is what caught the earlier mistake of reading 0.3 of noise as a result.

> **The result that was impossible, and what it actually meant.** The forecast first appeared to *beat* perfect weather. That cannot happen. Adding a same-source control resolved it: Open-Meteo grid values plus cloud cover beat single SMHI station readings by 0.67. The forecast was not beating reality — it was beating a worse measurement of reality. Within one provider, perfect >= forecast, as it must be.

## Guarding the feature builder

**File:** `tests/test_features.py` — _197 lines · 13 tests, all offline_

Several of these are regression tests for bugs that were caught during development. Each would have corrupted training data silently.

| Test | The mistake it locks out |
|---|---|
| `cutoff_stays_11_local_across_the_25_hour_day` | The cutoff drifting to 12:00 on clock-change days |
| `drops_target_hours_whose_lag_lands_in_their_own_delivery_day` | A 24h lag reaching a price from the same unpublished auction |
| `price_grid_prefers_the_shorter_overlapping_period` | The DST overlap resolving the wrong way |
| `weather_features_are_not_silently_all_null` | A dropped timezone matching 0 of 1,083 rows without raising |
| `forecast_features_read_the_target_hour_at_the_right_lead` | A forecast column quietly carrying the wrong lead time |
| `forecast_modes_never_expose_the_analysed_value` | lead 0 — the actual outcome — leaking into features |

## Layer 5 — the audit

**File:** `tests/test_ingestion.py` — _181 lines · 9 tests covering ingestion · all offline, about one second_

> **Say this**
>
> “The test suite runs entirely offline against synthetic payloads, so it works without network access and can't be broken by an API being down. It's the same approach as the reference platform's test package.”

### How a test is built

```python
PRICES_PAYLOAD = {"multiAreaEntries": [...]}   # a fake API response

def test_normalize_prices_hourly():
    rows = normalize_prices(PRICES_PAYLOAD)
    assert len(rows) == 8   # 2 periods x 4 areas
```

`assert` means “this must be true”. If it isn't, the test fails and names the line. Pytest finds every function starting with `test_` automatically.

### What each test is actually protecting

| Test | The mistake it would catch |
|---|---|
| `normalize_prices_hourly` | Unpacking the nested payload wrongly — wrong row count. |
| `normalize_prices_quarter_hour` | Assuming hourly periods and mis-labelling the 15-minute market. |
| `schema_validate_orders_and_casts` | Columns arriving in a different order, or prices stored as text. |
| `data_handler_roundtrip` | **Duplicate rows on re-run** — it writes the same batch twice and asserts the count stays 8. |
| `smhi_archive_csv_parsing` | Metadata header handling, and the date filter letting old rows through. |
| `elpriset_normalize_converts…` | Forgetting the ×1000, or mishandling the timezone offset. |
| `dagster_definitions_load` | A broken asset graph — it imports the registry and checks the expected names exist. |

### The two techniques worth knowing

```text
monkeypatch.setattr(smhi_data, "_get", lambda url, max_retries=3: FakeResp())
```

**Monkeypatching** temporarily swaps a real function for a fake one, so a test that would normally call the internet reads a string instead. Pytest undoes it after the test.

```python
def test_data_handler_roundtrip(tmp_path, monkeypatch):
```

`tmp_path` is a throwaway folder pytest creates and deletes. The test writes a real parquet file and a real database — into a sandbox, never touching my actual data.

## The analysis script

**File:** `notebooks/03_eda.py` — _253 lines · reads the warehouse, writes reports/eda_data.json and six figures_

### Block 1 — `load()` and the pivot

```text
prices = px.pivot(index="h", columns="a", values="p")
```

A **pivot** turns long into wide — the single most useful reshaping operation in data work.

> **Before and after**
>
> ```text
> long (as stored)              wide (as analysed)
> hour   area  price            hour    SE1   SE2   SE3   SE4
> 10:00  SE1   25.5             10:00  25.5  25.5  43.0  49.0
> 10:00  SE2   25.5      →      11:00  22.1  22.1  38.4  44.2
> 10:00  SE3   43.0
> ```
> Long is right for *storing* (add an area, add rows). Wide is right for *comparing* (correlations need columns side by side).

### Block 2 — `bucket_means()`

```python
for lo, hi in zip(edges[:-1], edges[1:]):
    m = (x >= lo) & (x < hi)
    if m.sum() >= 50: ...
```

`zip(edges[:-1], edges[1:])` is a neat trick for turning a list of boundaries into consecutive pairs: `[-30,-10,-5]` becomes `(-30,-10)` then `(-10,-5)`.

`m` is a **boolean mask** — a true/false value per row. `y[m]` keeps only the true ones. The `>= 50` guard drops buckets too thin to mean anything, so I never report an average built from three observations.

### Block 3 — why it writes JSON

The script computes; the report renders. Splitting them means the visual summary is a pure display of numbers it didn't invent, and re-running the script updates the report without touching any chart code.

## The empty `__init__.py` files

There are several zero-byte files called `__init__.py`. They aren't leftovers.

Their presence tells Python “this folder is a package” — which is what makes `from ingestion.elpriset import elpriset_data` work. Without the empty file in `ingestion/elpriset/`, that import may fail depending on how the code is run.

They're allowed to contain setup code. Mine are deliberately empty, because importing a package shouldn't *do* anything.

## Questions one may ask

#### “Why two price sources?”

The free Nord Pool API only serves a rolling ~62-day window; anything older returns 401. That's enough for live daily collection but not for training history. So Nord Pool covers live, elpriset covers history, and they were cross-checked on 19,200 overlapping hours — exact agreement, correlation 1.0. A fourth source, Open-Meteo, supplies archived weather *forecasts*, which turned out to be the single most valuable feature group in the project.

#### “How do you know re-running won't corrupt the data?”

Writes are replace-per-partition, not append: the handler deletes the time range then inserts it. There's a test that persists the same batch twice and asserts the row count doesn't change.

#### “What happens if an API changes its format?”

Errors are caught, the raw payload is dumped to `data/raw/_debug/`, and the run continues. Honestly though — that only catches failures that raise. A renamed top-level field would return zero rows quietly, so I sanity-check counts after a backfill rather than trusting the exit code.

#### “Why is weather stored long instead of wide?”

Because per-parameter station overrides mean SE3's wind and SE3's temperature come from different stations. A wide row would imply one source for all six values. Long format keeps `station_id` on every value; the wide shape is produced later by pivoting.

#### “What would change in production?”

Storage would be Google Cloud Storage and BigQuery instead of local parquet and DuckDB; deployment would be Kubernetes instead of `dagster dev`; and the model would need weather *forecasts* rather than observations. The code structure — config, schemas, assets, factory, registry — would stay as it is.

#### “What do you know is still wrong?”

Four things, all written down. A single failed request aborts an entire backfill instead of skipping and reporting. Nord Pool's rolling window means old Dagster partitions age out — the count has already fallen from 65 to 56. Two of the four sources are not yet orchestrated, only run by hand. And the 24 observed-weather features cannot be shown to earn their place: measured contribution +0.07 EUR/MWh with an interval spanning zero, kept for now pending re-test once the model improves.

The daylight-saving overlap that used to be on this list is fixed: the hourly grid prefers the shorter of two overlapping periods, and building it surfaced two further leakage bugs that are now covered by regression tests.

Companion documents: docs/01_platform_conventions.md · docs/02_data_access.md · docs/03_eda.md 
Run the test suite with `pytest tests/` (22 tests) · open the pipeline UI with `dagster dev -f orchestration/definitions.py` 
Build the feature table with `PYTHONPATH=. python features/build_features.py` · reproduce the weather measurement with `PYTHONPATH=. OMP_NUM_THREADS=1 python models/weather_value_experiment.py`
