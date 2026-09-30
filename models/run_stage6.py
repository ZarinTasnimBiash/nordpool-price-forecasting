"""Stage 6 — validation and error analysis.

Run:  PYTHONPATH=. OMP_NUM_THREADS=1 python -u models/run_stage6.py

Stage 5 asked "is the model good?" and answered yes, on average. This asks the
harder questions: **where** is it bad, is it bad for reasons that make physical
sense, and is every feature earning its place?

Six checks, all on the ten reporting folds that were never used for tuning:

  1  error by hour of day        — Stage 4 found the evening peak twice as bad
  2  error by month and by price level — including spikes and negative hours
  3  permutation importance      — checked against the Stage 3 physics
  4  observed-weather ablation   — do those 24 columns earn their place yet?
  5  asymmetric conformal        — the interval is 5.5% below / 14.1% above
  6  extrapolation               — trees cannot predict above their training max
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from config.configs import STORAGE
from models import evaluation as ev
from models.predictors import (ConformalQuantileModel, SingleModel, TARGET,
                               feature_columns, naive)
from models.run_stage5 import load_features

OUT = STORAGE["duckdb_path"].parent.parent / "reports" / "stage6_results.json"

BEST = dict(learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=80)
WX = ("air_temperature", "wind_speed", "wind_from_direction", "relative_humidity",
      "precipitation_amount_1h", "air_pressure_at_sea_level", "cloud_cover")


def families(cols: list[str]) -> dict[str, list[str]]:
    """Group the 70 features the way the project talks about them."""
    obs = [c for c in cols if any(c.startswith(w) for w in WX)
           and ("_24h_mean" in c or "_24h_min" in c or "_24h_max" in c)]
    fc = [c for c in cols if c.endswith("_fc2")]
    lag = [c for c in cols if "_lag_" in c]
    roll = [c for c in cols if "_roll_" in c]
    cal = [c for c in cols if c not in obs + fc + lag + roll]
    return {"weather observed": obs, "weather forecast": fc,
            "price lags": lag, "price rolling": roll, "calendar": cal}


def permutation_importance(model, test: pd.DataFrame, cols: list[str],
                           n_repeats: int = 5, seed: int = 0) -> dict[str, float]:
    """How much worse does the model get if one feature is shuffled?

    Shuffling breaks the link between that column and the target while leaving
    everything else intact, so the rise in MAE is what the model was actually
    getting from it. Preferred over the tree-internal importance, which counts
    how often a feature was *split on* — that flatters high-cardinality columns
    regardless of whether the splits helped.
    """
    y = test[TARGET].to_numpy(float)
    base = float(np.abs(model.predict(test) - y).mean())
    rng = np.random.default_rng(seed)
    out = {}
    for c in cols:
        losses = []
        for _ in range(n_repeats):
            shuffled = test.copy()
            shuffled[c] = rng.permutation(shuffled[c].to_numpy())
            losses.append(float(np.abs(model.predict(shuffled) - y).mean()))
        out[c] = float(np.mean(losses) - base)
    return out


def main() -> None:
    df = load_features()
    folds = ev.make_folds(df.index)
    report = [f for f in folds if f["purpose"] == "report"]
    cols = feature_columns(df)
    fam = families(cols)
    print(f"features {len(cols)} | " + " ".join(f"{k}={len(v)}" for k, v in fam.items()))
    payload = {"n_features": len(cols),
               "families": {k: len(v) for k, v in fam.items()}}

    # ---- fit the shipped model once per reporting fold ---------------------
    print("\nfitting the shipped model on each reporting fold")
    models, P, Y, IDX, TRMAX = [], [], [], [], []
    for f in report:
        tr, te = df[f["train"]], df[f["test"]]
        m = SingleModel(**BEST).fit(tr)
        models.append((m, te))
        P.append(m.predict(te)); Y.append(te[TARGET].to_numpy(float)); IDX.append(te.index)
        TRMAX.append(float(tr[TARGET].max()))
        print(f"  {f['label']}  trees {m.n_trees_}")
    pred = np.concatenate(P); y = np.concatenate(Y); idx = IDX[0].append(IDX[1:])
    err = np.abs(pred - y)
    local = idx.tz_convert(ev.LOCAL_TZ)
    naive_pred = np.concatenate([df[f["test"]]["spot_price_se3_lag_24h"].to_numpy(float)
                                 for f in report])
    naive_err = np.abs(naive_pred - y)
    print(f"\npooled MAE {err.mean():.2f}  (naive {naive_err.mean():.2f})")

    # ---- 1. error by hour of day ------------------------------------------
    print("\n1 · ERROR BY HOUR OF DAY (local)")
    by_hour = pd.DataFrame({"hour": local.hour, "err": err, "naive": naive_err,
                            "y": y}).groupby("hour").agg(
        MAE=("err", "mean"), naive_MAE=("naive", "mean"), mean_price=("y", "mean"))
    by_hour["gain_pct"] = 100 * (by_hour.naive_MAE - by_hour.MAE) / by_hour.naive_MAE
    print(by_hour.round(2).to_string())
    payload["by_hour"] = by_hour.round(3).to_dict("index")
    worst = by_hour.MAE.idxmax(); best = by_hour.MAE.idxmin()
    print(f"  worst hour {worst:02d}:00 MAE {by_hour.MAE.max():.2f}  |  "
          f"best {best:02d}:00 MAE {by_hour.MAE.min():.2f}  |  "
          f"ratio {by_hour.MAE.max()/by_hour.MAE.min():.2f}x")

    # ---- 2. error by month, and by price level ----------------------------
    print("\n2a · ERROR BY MONTH")
    by_month = pd.DataFrame({"m": local.strftime("%Y-%m"), "err": err,
                             "naive": naive_err, "y": y}).groupby("m").agg(
        MAE=("err", "mean"), naive_MAE=("naive", "mean"), mean_price=("y", "mean"))
    by_month["gain_pct"] = 100 * (by_month.naive_MAE - by_month.MAE) / by_month.naive_MAE
    print(by_month.round(2).to_string())
    payload["by_month"] = by_month.round(3).to_dict("index")

    print("\n2b · ERROR BY PRICE LEVEL (where the money is)")
    edges = [-np.inf, 0, 20, 50, 100, 200, np.inf]
    names = ["negative", "0-20", "20-50", "50-100", "100-200", "200+"]
    bucket = pd.cut(y, edges, labels=names)
    by_price = pd.DataFrame({"b": bucket, "err": err, "naive": naive_err, "y": y}).groupby(
        "b", observed=False).agg(n=("err", "size"), MAE=("err", "mean"),
                                 naive_MAE=("naive", "mean"), mean_price=("y", "mean"))
    by_price["share_of_total_error_pct"] = 100 * (
        pd.Series(err).groupby(bucket, observed=False).sum() / err.sum()).values
    print(by_price.round(2).to_string())
    payload["by_price"] = by_price.round(3).fillna(0).to_dict("index")

    # bias: does the model systematically under- or over-shoot?
    signed = pred - y
    print(f"\n  mean signed error {signed.mean():+.2f} "
          f"(negative = the model runs low)")
    payload["mean_signed_error"] = float(signed.mean())

    # ---- 3. permutation importance ----------------------------------------
    print("\n3 · PERMUTATION IMPORTANCE (mean MAE rise when shuffled, over folds)")
    imps = [permutation_importance(m, te, cols) for m, te in models]
    imp = pd.Series({c: float(np.mean([d[c] for d in imps])) for c in cols}
                    ).sort_values(ascending=False)
    print(imp.head(15).round(3).to_string())
    payload["importance_top20"] = imp.head(20).round(4).to_dict()
    fam_imp = {k: float(imp[v].sum()) for k, v in fam.items()}
    print("\n  by family (summed):")
    for k, v in sorted(fam_imp.items(), key=lambda kv: -kv[1]):
        print(f"    {k:20s} {v:7.3f}")
    payload["importance_by_family"] = {k: round(v, 4) for k, v in fam_imp.items()}

    # the Stage 3 claim: northern wind beats local wind for SE3
    for a, b in [("wind_speed_se1_fc2", "wind_speed_se3_fc2")]:
        if a in imp and b in imp:
            print(f"\n  physics check — {a} {imp[a]:+.3f} vs {b} {imp[b]:+.3f}"
                  f"  -> {'northern' if imp[a] > imp[b] else 'local'} wind matters more")
            payload["wind_check"] = {a: round(float(imp[a]), 4), b: round(float(imp[b]), 4)}

    # ---- 4. does observed weather earn its place yet? ----------------------
    print("\n4 · ABLATION — drop the 24 observed-weather columns")
    keep = [c for c in cols if c not in fam["weather observed"]]
    P2 = []
    for f in report:
        tr, te = df[f["train"]], df[f["test"]]
        sub_tr = tr[keep + [TARGET]]
        m = SingleModel(**BEST).fit(sub_tr)
        P2.append(m.predict(te[keep + [TARGET]]))
    pred_no_obs = np.concatenate(P2)
    print(f"  with observed weather   MAE {err.mean():.3f}")
    print(f"  without                 MAE {np.abs(pred_no_obs - y).mean():.3f}")
    cmp = ev.bootstrap_difference(y, pred_no_obs, pred, idx)
    print(f"  keeping them is worth  {cmp['diff']:+.3f}  95% CI "
          f"[{cmp['lo']:+.3f}, {cmp['hi']:+.3f}]  {ev.verdict(cmp)}")
    payload["observed_weather_ablation"] = {
        "MAE_with": float(err.mean()), "MAE_without": float(np.abs(pred_no_obs - y).mean()),
        **cmp}

    # ---- 5. asymmetric conformal ------------------------------------------
    print("\n5 · INTERVALS — one pad for both sides, or one per side?")
    out5 = {}
    for label, sym in (("symmetric", True), ("asymmetric", False)):
        lo, mid, hi, pads = [], [], [], []
        for f in report:
            tr, te = df[f["train"]], df[f["test"]]
            c = ConformalQuantileModel(SingleModel, symmetric=sym, **BEST).fit(tr)
            pr = c.predict(te)
            lo.append(pr[0.1]); mid.append(pr[0.5]); hi.append(pr[0.9])
            pads.append((c.pad_lo_, c.pad_hi_))
        cov = ev.interval_coverage(y, np.concatenate(lo), np.concatenate(hi))
        print(f"  {label:11s} coverage {100*cov['coverage']:.1f}%  "
              f"(below {100*cov['below']:.1f}%, above {100*cov['above']:.1f}%)  "
              f"width {cov['mean_width']:.1f}")
        out5[label] = {"coverage": cov,
                       "pads": [[round(a, 2), round(b, 2)] for a, b in pads]}
    payload["intervals"] = out5

    # ---- 6. extrapolation --------------------------------------------------
    print("\n6 · EXTRAPOLATION — trees cannot predict above their training max")
    above = np.concatenate([Y[i] > TRMAX[i] for i in range(len(report))])
    print(f"  test hours whose true price exceeded the training maximum: "
          f"{above.sum()} of {len(y)} ({100*above.mean():.2f}%)")
    if above.any():
        print(f"  MAE on those hours {err[above].mean():.1f} "
              f"vs {err[~above].mean():.1f} elsewhere")
    payload["extrapolation"] = {
        "n_above_train_max": int(above.sum()), "n_total": int(len(y)),
        "pct": float(100 * above.mean()),
        "MAE_above": float(err[above].mean()) if above.any() else None,
        "MAE_below": float(err[~above].mean())}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, default=float))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
