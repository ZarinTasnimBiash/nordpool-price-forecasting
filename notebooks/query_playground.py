"""A place to try SQL queries against the warehouse.

HOW TO RUN THIS FILE
--------------------
From the project root, in the terminal:

    PYTHONPATH=. python notebooks/query_playground.py

Then edit a query below, save, and run it again. Nothing here can damage the
data: the connection is opened read-only.
"""

import duckdb
import pandas as pd

from config.configs import STORAGE

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 50)

# read_only=True means "I am only looking". It also lets you query while
# something else (a Dagster run, say) is writing.
con = duckdb.connect(str(STORAGE["duckdb_path"]), read_only=True)


def q(sql: str, title: str = "") -> pd.DataFrame:
    """Run some SQL, print the answer, and hand back the table."""
    df = con.execute(sql).df()
    print(f"\n{'=' * 70}\n{title or sql.strip().splitlines()[0]}\n{'=' * 70}")
    print(df.to_string(index=False))
    return df


# ---------------------------------------------------------------- 0. look around
q("show tables", "0 · what tables exist")

q("describe elpriset_day_ahead_prices", "0b · what columns the price table has")


# ------------------------------------------------------------ 1. simplest query
q("""
    select *
    from elpriset_day_ahead_prices
    limit 5
""", "1 · just show me five rows")


# ------------------------------------------------------------- 2. group and count
q("""
    select delivery_area,
           round(avg(price_eur_mwh), 1) as avg_price,
           count(*)                     as periods
    from elpriset_day_ahead_prices
    group by delivery_area
    order by avg_price
""", "2 · average price per bidding area (the north-south split)")


# ------------------------------------------------- 3. UTC storage, local question
# Everything is stored in UTC. To ask a human question ("which hour of the day
# is dearest?") convert to local time first with `at time zone`.
q("""
    select extract(hour from delivery_start_utc at time zone 'Europe/Stockholm') as hour_local,
           round(avg(price_eur_mwh), 1) as avg_price
    from elpriset_day_ahead_prices
    where delivery_area = 'SE3'
    group by hour_local
    order by hour_local
""", "3 · SE3 average price by hour of day, local time")


# ------------------------------------------------------------- 4. the 15-min trap
# Since 2025-10-01 the market trades in 15-minute periods, stored natively.
# Asking for `= 18:00` gives you the FIRST QUARTER of that hour, not the hour.
# Use a RANGE and average, which is what the feature builder does.
q("""
    select delivery_start_utc, resolution_minutes, price_eur_mwh
    from elpriset_day_ahead_prices
    where delivery_area = 'SE3'
      and delivery_start_utc >= timestamp with time zone '2026-01-15 18:00:00+01'
      and delivery_start_utc <  timestamp with time zone '2026-01-15 19:00:00+01'
    order by delivery_start_utc
""", "4 · the four quarter-hours inside 18:00-19:00 on 15 Jan 2026")

q("""
    select round(avg(price_eur_mwh), 2) as hourly_price
    from elpriset_day_ahead_prices
    where delivery_area = 'SE3'
      and delivery_start_utc >= timestamp with time zone '2026-01-15 18:00:00+01'
      and delivery_start_utc <  timestamp with time zone '2026-01-15 19:00:00+01'
""", "4b · averaged into one hour — matches the model's target of 104.42")


# --------------------------------------------------------------- 5. long weather
# Weather is stored LONG: one row per (time, area, parameter), because SE3 wind
# and SE3 temperature come from different stations. So you FILTER on parameter
# rather than selecting a column.
q("""
    select parameter, count(*) as readings, round(avg(value), 1) as avg_value
    from weather_smhi_data
    where area = 'SE3'
    group by parameter
    order by parameter
""", "5 · what weather parameters exist for SE3")


# ------------------------------------------------------------ 6. the model's table
q("""
    select hour_utc,
           spot_price_se3_lag_24h,
           air_temperature_se3_fc2,
           wind_speed_se3_fc2,
           target_spot_price_se3
    from features_se3_hourly
    order by hour_utc desc
    limit 5
""", "6 · the most recent rows the model was trained on")


# ============================================================ YOUR TURN
# Copy any block above, change it, and run the file again. Ideas:
#   - swap 'SE3' for 'SE4' in query 3 — is the evening peak bigger in the south?
#   - in query 5, change area to 'SE1'
#   - count negative-price hours:  where price_eur_mwh < 0
#   - average price per month:     date_trunc('month', delivery_start_utc)

q("""
    select count(*) as negative_price_periods
    from elpriset_day_ahead_prices
    where delivery_area = 'SE3' and price_eur_mwh < 0
""", "7 · YOUR TURN — edit me: how many SE3 periods had a negative price?")

con.close()
