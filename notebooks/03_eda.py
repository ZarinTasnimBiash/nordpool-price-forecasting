"""Stage 3 — exploratory data analysis.

Reads the DuckDB warehouse, builds an hourly price/weather matrix, computes the
profiles and relationships documented in docs/03_eda.md, and writes:

  reports/eda_data.json     aggregates (feeds reports/03_eda.html)
  reports/figures/*.png     static charts embedded in docs/03_eda.md

Run:  PYTHONPATH=. python notebooks/03_eda.py
"""

from __future__ import annotations

import json

import duckdb
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # no display needed
import matplotlib.pyplot as plt  # noqa: E402

from config.configs import BIDDING_AREAS, STORAGE  # noqa: E402

TARGET = "SE3"
REPORTS = STORAGE["duckdb_path"].parent.parent / "reports"
OUT = REPORTS / "eda_data.json"
FIGDIR = REPORTS / "figures"

# Same categorical palette as the HTML report, so both tell one visual story.
AREA_COLOURS = {"SE1": "#2a78d6", "SE2": "#eb6834", "SE3": "#1baf7a", "SE4": "#eda100"}
PRIMARY, SECONDARY, NEGATIVE, MUTED = "#2a78d6", "#eb6834", "#d03b3b", "#6b7780"


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hourly price matrix (area columns) and hourly weather matrix."""
    con = duckdb.connect(str(STORAGE["duckdb_path"]), read_only=True)
    con.execute("set enable_progress_bar=false")
    # Sub-hourly periods are averaged into their UTC hour. NOTE: on the 3 DST
    # fall-back days the source overlaps one hour (see docs/03_eda.md); that
    # hour is therefore a blend. Stage 4 must resolve it explicitly.
    px = con.execute("""
        select date_trunc('hour', delivery_start_utc) as h,
               delivery_area as a, avg(price_eur_mwh) as p
        from elpriset_day_ahead_prices group by 1, 2
    """).df()
    wx = con.execute("""
        select date_trunc('hour', time_utc) as h, area, parameter, avg(value) as v
        from weather_smhi_data group by 1, 2, 3
    """).df()
    con.close()

    px["h"] = pd.to_datetime(px["h"], utc=True)
    prices = px.pivot(index="h", columns="a", values="p").sort_index()

    wx["h"] = pd.to_datetime(wx["h"], utc=True)
    weather = wx.pivot_table(index="h", columns=["area", "parameter"], values="v")
    weather.columns = [f"{p}_{a.lower()}" for a, p in weather.columns]
    return prices, weather.sort_index()


def bucket_means(x: pd.Series, y: pd.Series, edges: list[float]) -> list[dict]:
    """Mean of y within each [lo, hi) bucket of x, dropping thin buckets."""
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (x >= lo) & (x < hi)
        if m.sum() >= 50:
            out.append({"lo": lo, "hi": hi, "mean": round(y[m].mean(), 1),
                        "n": int(m.sum())})
    return out


def _style(ax, title: str, ylabel: str = "") -> None:
    """Shared chart chrome: recessive grid, no box, muted labels."""
    ax.set_title(title, fontsize=11, fontweight="600", loc="left", pad=10)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    ax.grid(axis="y", color="#e3eaee", linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(colors=MUTED, labelsize=9, length=0)


def make_figures(local: pd.DataFrame, payload: dict) -> None:
    """Write the static charts embedded in docs/03_eda.md.

    Deliberately light-background: GitHub renders these on both its light and
    dark themes, and a fixed light figure stays legible on either.
    """
    FIGDIR.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.facecolor": "white", "axes.facecolor": "white",
                         "savefig.facecolor": "white", "font.size": 10})

    # 1 — three years, 30-day rolling mean (the seasonal shape)
    fig, ax = plt.subplots(figsize=(10, 3.6))
    daily = local.resample("1D").mean().rolling(30, center=True, min_periods=1).mean()
    for area in BIDDING_AREAS:
        ax.plot(daily.index, daily[area], color=AREA_COLOURS[area], linewidth=1.6, label=area)
    ax.legend(frameon=False, fontsize=9, ncol=4, loc="upper left")
    _style(ax, "Day-ahead price by bidding zone — 30-day rolling mean", "EUR/MWh")
    fig.tight_layout(); fig.savefig(FIGDIR / "01_price_history.png", dpi=150); plt.close(fig)

    # 2 — hour-of-day profile, weekday vs weekend
    fig, ax = plt.subplots(figsize=(7, 3.4))
    hours = [r["hour"] for r in payload["hour_profile"]]
    ax.plot(hours, [r["weekday"] for r in payload["hour_profile"]],
            color=PRIMARY, linewidth=2.2, marker="o", markersize=3.5, label="Mon–Fri")
    ax.plot(hours, [r["weekend"] for r in payload["hour_profile"]],
            color=SECONDARY, linewidth=2.2, marker="o", markersize=3.5, label="Sat–Sun")
    ax.set_xticks(range(0, 24, 3))
    ax.legend(frameon=False, fontsize=9)
    _style(ax, f"{TARGET} average price by hour of day (local time)", "EUR/MWh")
    fig.tight_layout(); fig.savefig(FIGDIR / "02_hour_profile.png", dpi=150); plt.close(fig)

    # 3 — month of year
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.bar([r["month"] for r in payload["month"]], [r["mean"] for r in payload["month"]],
           color=PRIMARY, width=0.68)
    _style(ax, f"{TARGET} average price by calendar month", "EUR/MWh")
    fig.tight_layout(); fig.savefig(FIGDIR / "03_month_profile.png", dpi=150); plt.close(fig)

    # 4 — distribution, negative bins highlighted
    fig, ax = plt.subplots(figsize=(7, 3.2))
    h = payload["hist"]
    ax.bar([(r["x0"] + r["x1"]) / 2 for r in h], [r["n"] for r in h],
           width=(h[0]["x1"] - h[0]["x0"]) * 0.9,
           color=[NEGATIVE if r["x1"] <= 0 else PRIMARY for r in h])
    ax.axvline(0, color=MUTED, linestyle="--", linewidth=1)
    _style(ax, f"Distribution of {TARGET} hourly prices (red = below zero)", "hours")
    ax.set_xlabel("EUR/MWh", fontsize=9, color=MUTED)
    fig.tight_layout(); fig.savefig(FIGDIR / "04_distribution.png", dpi=150); plt.close(fig)

    # 5 — the two weather drivers, side by side on their own scales
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.2))
    tb = payload["temp_bins"]
    axes[0].bar([f'{r["lo"]}…{r["hi"]}' for r in tb], [r["mean"] for r in tb], color=PRIMARY)
    _style(axes[0], f"{TARGET} price by temperature (°C)", "EUR/MWh")
    axes[0].tick_params(axis="x", labelrotation=45, labelsize=8)
    wb = payload["wind_bins"]
    axes[1].bar([f'{r["lo"]}–{r["hi"]}' for r in wb], [r["mean"] for r in wb], color=PRIMARY)
    _style(axes[1], f"{TARGET} price by wind speed (m/s)", "EUR/MWh")
    fig.tight_layout(); fig.savefig(FIGDIR / "05_weather.png", dpi=150); plt.close(fig)

    # 6 — autocorrelation, with the leakage boundary marked
    fig, ax = plt.subplots(figsize=(7, 3.2))
    acf = payload["acf"]
    labels = [f'{r["lag"]}h' for r in acf]
    ax.bar(labels, [r["r"] for r in acf],
           color=["#d5dde2" if r["lag"] < 24 else PRIMARY for r in acf])
    cutoff = next(i for i, r in enumerate(acf) if r["lag"] >= 24)
    ax.axvline(cutoff - 0.5, color=NEGATIVE, linestyle="--", linewidth=1.3)
    ax.text(cutoff - 0.35, 0.95, "usable at 11:00 →", color=NEGATIVE, fontsize=8.5, va="top")
    _style(ax, f"Autocorrelation of {TARGET} price by lag (grey = unavailable)", "correlation")
    fig.tight_layout(); fig.savefig(FIGDIR / "06_autocorrelation.png", dpi=150); plt.close(fig)

    print(f"wrote {len(list(FIGDIR.glob('*.png')))} figures -> {FIGDIR}")


def main() -> None:
    prices, weather = load()
    local = prices.tz_convert("Europe/Stockholm")
    se3 = local[TARGET]
    joined = prices.join(weather, how="inner")

    # --- distribution per area ------------------------------------------------
    area_stats = []
    for a in BIDDING_AREAS:
        s = prices[a]
        area_stats.append({
            "area": a, "mean": round(s.mean(), 1), "median": round(s.median(), 1),
            "p5": round(s.quantile(.05), 1), "p95": round(s.quantile(.95), 1),
            "min": round(s.min(), 1), "max": round(s.max(), 1),
            "pct_negative": round((s < 0).mean() * 100, 1),
        })

    # --- profiles -------------------------------------------------------------
    is_weekend = se3.index.dayofweek >= 5
    hour_profile = [
        {"hour": int(h),
         "weekday": round(se3[~is_weekend].groupby(se3[~is_weekend].index.hour).mean()[h], 1),
         "weekend": round(se3[is_weekend].groupby(se3[is_weekend].index.hour).mean()[h], 1)}
        for h in range(24)
    ]
    dow = se3.groupby(se3.index.dayofweek).mean().round(1)
    month = se3.groupby(se3.index.month).mean().round(1)

    # --- daily series for the time-series chart -------------------------------
    daily = local.resample("1D").mean().round(1)
    daily_series = [
        {"d": d.strftime("%Y-%m-%d"),
         **{a: (None if pd.isna(v) else float(v)) for a, v in row.items()}}
        for d, row in daily.iterrows()
    ]

    # --- distribution histogram (SE3) ----------------------------------------
    lo, hi = -60, 260
    counts, edges = np.histogram(se3.clip(lo, hi), bins=32, range=(lo, hi))
    hist = [{"x0": round(edges[i], 1), "x1": round(edges[i + 1], 1),
             "n": int(counts[i])} for i in range(len(counts))]

    # --- weather relationships ------------------------------------------------
    temp_bins = bucket_means(joined["air_temperature_se3"], joined[TARGET],
                             [-30, -10, -5, 0, 5, 10, 15, 20, 40])
    wind_bins = bucket_means(joined["wind_speed_se3"], joined[TARGET],
                             [0, 2, 4, 6, 8, 25])

    weather_corr = (joined.corr(numeric_only=True)[TARGET]
                    .drop(BIDDING_AREAS).sort_values())
    corr_list = [{"feature": k, "r": round(v, 3)}
                 for k, v in weather_corr.items() if abs(v) >= 0.10]

    # --- lag structure --------------------------------------------------------
    acf = [{"lag": L, "r": round(se3.autocorr(L), 3)}
           for L in [1, 2, 3, 6, 12, 24, 48, 72, 168, 336]]

    payload = {
        "meta": {
            "hours": int(len(prices)),
            "start": str(prices.index.min().date()),
            "end": str(prices.index.max().date()),
            "target": TARGET,
        },
        "area_stats": area_stats,
        "area_corr": prices.corr().round(3).to_dict(),
        "daily": daily_series,
        "hour_profile": hour_profile,
        "dow": [{"dow": d, "mean": float(dow[i])} for i, d in
                enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])],
        "month": [{"month": m, "mean": float(month[i])} for i, m in
                  enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                             "Aug", "Sep", "Oct", "Nov", "Dec"], 1)],
        "hist": hist,
        "temp_bins": temp_bins,
        "wind_bins": wind_bins,
        "weather_corr": corr_list,
        "acf": acf,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1))
    make_figures(local, payload)
    print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
    print(f"  {payload['meta']['hours']:,} hours, {payload['meta']['start']} .. {payload['meta']['end']}")
    print(f"  SE3 mean {area_stats[2]['mean']} EUR/MWh, {area_stats[2]['pct_negative']}% negative hours")
    print(f"  peak hour {max(hour_profile, key=lambda r: r['weekday'])['hour']}:00 weekday")
    print(f"  lag-24h autocorrelation {dict((a['lag'], a['r']) for a in acf)[24]}")


if __name__ == "__main__":
    main()
