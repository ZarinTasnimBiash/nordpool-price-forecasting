"""Stage 5 — walk-forward evaluation of baselines and models.

Run:  OMP_NUM_THREADS=1 python -u models/run_stage5.py [--quick] [--step5]

Every number produced here comes from folds that roll forward in time. The
first five folds are used for tuning; the rest are never looked at until the
final report, so they stay a genuine out-of-sample test.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import duckdb
import numpy as np
import pandas as pd

from config.configs import STORAGE
from models import evaluation as ev
from models.predictors import (ConformalQuantileModel, GroupedModel, LinearModel,
                               QuantileModel, SingleModel, TARGET, naive, train_mean)

OUT = STORAGE["duckdb_path"].parent.parent / "reports" / "stage5_results.json"


def load_features() -> pd.DataFrame:
    con = duckdb.connect(str(STORAGE["duckdb_path"]), read_only=True)
    con.execute("set enable_progress_bar=false")
    con.execute("set threads=1")          # reproducibility; see build_features
    df = con.execute("select * from features_se3_hourly order by hour_utc").df()
    con.close()
    df["hour_utc"] = pd.to_datetime(df["hour_utc"], utc=True)
    return df.set_index("hour_utc")


# --------------------------------------------------------------- baselines
BASELINES = {
    "naive: yesterday same hour": lambda tr, te: naive(te, "spot_price_se3_lag_24h"),
    "naive: last week same hour": lambda tr, te: naive(te, "spot_price_se3_lag_168h"),
    "naive: training mean":       train_mean,
}


def run_folds(df: pd.DataFrame, folds: list[dict], builders: dict,
              only: str | None = None) -> dict:
    """Fit and predict every builder on every fold; keep predictions per fold."""
    results = {name: {} for name in builders}
    for i, fold in enumerate(folds, 1):
        if only and fold["purpose"] != only:
            continue
        tr, te = df[fold["train"]], df[fold["test"]]
        for name, build in builders.items():
            t0 = time.time()
            pred = build(tr, te)
            results[name][fold["label"]] = {
                "pred": np.asarray(pred, float),
                "y": te[TARGET].to_numpy(float),
                "index": te.index,
                "secs": time.time() - t0,
            }
        print(f"  fold {i:2d}/{len(folds)}  {fold['label']}  "
              f"train {len(tr):6,}  test {len(te):5,}  [{fold['purpose']}]")
    return results


def summarise(results: dict, folds: list[dict], purpose: str = "report") -> dict:
    """Pool the chosen folds and score them, per hour and per day."""
    labels = [f["label"] for f in folds if f["purpose"] == purpose]
    out = {}
    for name, per_fold in results.items():
        parts = [per_fold[l] for l in labels if l in per_fold]
        if not parts:
            continue
        y = np.concatenate([p["y"] for p in parts])
        pred = np.concatenate([p["pred"] for p in parts])
        idx = parts[0]["index"].append([p["index"] for p in parts[1:]])
        fold_maes = [float(np.abs(p["pred"] - p["y"]).mean()) for p in parts]
        out[name] = {
            **ev.per_hour(y, pred),
            **ev.per_day(y, pred, idx),
            # kept per fold, not just summarised: "does it win on every fold?"
            # is a different and more useful question than "does it win on
            # average?", and it cannot be recovered from a mean and an sd.
            "fold_MAE": {l: m for l, m in zip(
                [l for l in labels if l in per_fold], fold_maes)},
            "fold_MAE_mean": float(np.mean(fold_maes)),
            "fold_MAE_sd": float(np.std(fold_maes)),
            "fold_MAE_min": float(np.min(fold_maes)),
            "fold_MAE_max": float(np.max(fold_maes)),
            "n_folds": len(parts),
            "_y": y, "_pred": pred, "_idx": idx,
        }
    return out


def table(summary: dict, title: str) -> None:
    print(f"\n{title}")
    print(f"  {'':34s} {'MAE':>7s} {'RMSE':>7s} │ {'fold MAE':>8s} {'sd':>5s} "
          f"{'min':>5s} {'max':>5s} │ {'med day':>7s} {'p90 day':>7s}")
    for name, s in summary.items():
        print(f"  {name:34s} {s['MAE']:7.2f} {s['RMSE']:7.2f} │ "
              f"{s['fold_MAE_mean']:8.2f} {s['fold_MAE_sd']:5.2f} "
              f"{s['fold_MAE_min']:5.1f} {s['fold_MAE_max']:5.1f} │ "
              f"{s['median_day_MAE']:7.2f} {s['p90_day_MAE']:7.2f}")


def step5_intervals(df: pd.DataFrame, folds: list[dict], winner_cls, best: dict) -> dict:
    """Fit the quantile version of the winner and check the intervals are honest.

    Two variants, because the first one is not good enough on its own:
    the raw quantile models, and the same models with their width corrected
    against held-out data (conformal). Both are reported — a correction is only
    believable next to the number it corrected.
    """
    print("\nSTEP 5 · quantile version of the winner — do 80% intervals hold 80%?")
    raw = {"lo": [], "mid": [], "hi": []}
    con = {"lo": [], "mid": [], "hi": []}
    ys, idxs, pads = [], [], []

    for fold in [f for f in folds if f["purpose"] == "report"]:
        tr, te = df[fold["train"]], df[fold["test"]]

        q = QuantileModel(winner_cls, quantiles=(0.1, 0.5, 0.9), **best).fit(tr)
        pr = q.predict(te)
        raw["lo"].append(pr[0.1]); raw["mid"].append(pr[0.5]); raw["hi"].append(pr[0.9])

        c = ConformalQuantileModel(winner_cls, **best).fit(tr)
        pc = c.predict(te)
        con["lo"].append(pc[0.1]); con["mid"].append(pc[0.5]); con["hi"].append(pc[0.9])
        pads.append(c.pad_)

        ys.append(te[TARGET].to_numpy(float)); idxs.append(te.index)
        print(f"  fold {fold['label']}  done   conformal pad {c.pad_:+6.2f}")

    yq = np.concatenate(ys)
    out = {}
    for label, d in (("quantile", raw), ("conformal", con)):
        lo = np.concatenate(d["lo"]); mid = np.concatenate(d["mid"]); hi = np.concatenate(d["hi"])
        cov = ev.interval_coverage(yq, lo, hi)
        qs = ev.per_hour(yq, mid)
        out[label] = {
            "coverage": cov, "median_scores": qs,
            "pinball": {str(qq): ev.pinball_loss(yq, pp, qq)
                        for qq, pp in [(0.1, lo), (0.5, mid), (0.9, hi)]},
        }
        print(f"\n  {label:10s} stated 80%  ->  actual coverage "
              f"{100*cov['coverage']:.1f}%   (below {100*cov['below']:.1f}%, "
              f"above {100*cov['above']:.1f}%)")
        print(f"  {'':10s} mean width {cov['mean_width']:.1f} EUR/MWh   "
              f"median as a point forecast: MAE {qs['MAE']:.2f}")

    out["conformal"]["pads"] = [float(p) for p in pads]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="baselines only")
    ap.add_argument("--step5", action="store_true",
                    help="re-run only the interval step, reusing the saved "
                         "hyperparameters and winner from a previous full run")
    args = ap.parse_args()

    df = load_features()
    folds = ev.make_folds(df.index)
    print(f"features: {len(df):,} rows x {len(df.columns)-1} columns")
    print(f"span    : {df.index.min().date()} .. {df.index.max().date()}")
    print(f"folds   : {ev.describe_folds(folds, df.index)}\n")

    if args.step5:
        payload = json.loads(OUT.read_text())
        best = payload["tuning"]["chosen"]
        winner_cls = GroupedModel if payload["winner"] == "grouped" else SingleModel
        print(f"re-running step 5 only, reusing {payload['winner']} model {best}")
        payload["quantile"] = step5_intervals(df, folds, winner_cls, best)
        OUT.write_text(json.dumps(payload, indent=1, default=float))
        print(f"\nwrote {OUT}")
        return

    # ---- step 1 + 2: baselines on every fold ----------------------------
    print("STEP 1-2 · baselines across all folds")
    base_res = run_folds(df, folds, BASELINES)
    base_tune = summarise(base_res, folds, "tune")
    base_rep = summarise(base_res, folds, "report")
    table(base_tune, "baselines — TUNING folds (used for choosing settings)")
    table(base_rep, "baselines — REPORTING folds (untouched)")

    payload = {
        "meta": {
            "rows": len(df), "features": len(df.columns) - 1,
            "span": [str(df.index.min()), str(df.index.max())],
            "folds": ev.describe_folds(folds, df.index),
            "fold_labels": {p: [f["label"] for f in folds if f["purpose"] == p]
                            for p in ("tune", "report")},
        },
        "baselines_report": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                             for k, v in base_rep.items()},
    }
    if args.quick:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=1))
        print(f"\nwrote {OUT}")
        return

    # ---- step 3a: tune the single model on the TUNING folds only --------
    print("\nSTEP 3 · tuning on the first 5 folds (reporting folds untouched)")
    grid = [
        dict(learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40),
        dict(learning_rate=0.05, max_leaf_nodes=63, min_samples_leaf=40),
        dict(learning_rate=0.03, max_leaf_nodes=31, min_samples_leaf=20),
        dict(learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=60),
        dict(learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=80),
        dict(learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=40,
             l2_regularization=10.0),
    ]
    tune_folds = [f for f in folds if f["purpose"] == "tune"]
    best, best_mae = None, np.inf
    for cfg in grid:
        maes, trees = [], []
        for fold in tune_folds:
            tr, te = df[fold["train"]], df[fold["test"]]
            m = SingleModel(**cfg).fit(tr)
            maes.append(float(np.abs(m.predict(te) - te[TARGET].to_numpy()).mean()))
            trees.append(m.n_trees_)
        mae = float(np.mean(maes))
        flag = ""
        if mae < best_mae:
            best, best_mae, flag = cfg, mae, "  <-- best so far"
        print(f"  {str(cfg):72s} MAE {mae:6.2f}  trees~{int(np.mean(trees)):4d}{flag}")
    print(f"\n  chosen: {best}")
    payload["tuning"] = {"grid_size": len(grid), "chosen": best,
                         "tune_MAE": round(best_mae, 3)}

    # ---- step 3c: the linear control, tuned on the same folds --------------
    print("\nSTEP 3c · ridge control on the same 70 features (same tuning folds)")
    best_alpha, best_alpha_mae = None, np.inf
    # grid deliberately extends past the optimum on both sides — a best value
    # sitting at the edge of its grid is a statement about the grid, not the model
    for alpha in (1.0, 10.0, 100.0, 1000.0, 3000.0, 10000.0):
        maes = []
        for fold in tune_folds:
            tr, te = df[fold["train"]], df[fold["test"]]
            m = LinearModel(alpha=alpha).fit(tr)
            maes.append(float(np.abs(m.predict(te) - te[TARGET].to_numpy()).mean()))
        mae = float(np.mean(maes))
        flag = ""
        if mae < best_alpha_mae:
            best_alpha, best_alpha_mae, flag = alpha, mae, "  <-- best so far"
        print(f"  alpha {alpha:8.1f}   MAE {mae:6.2f}{flag}")
    print(f"\n  chosen: alpha={best_alpha}")
    payload["tuning_linear"] = {"chosen_alpha": best_alpha,
                                "tune_MAE": round(best_alpha_mae, 3)}

    # ---- step 3b + 4: single vs grouped on the REPORTING folds ---------
    print("\nSTEP 3-4 · single vs grouped, on the untouched folds")
    builders = {
        **BASELINES,
        "model: ridge":   lambda tr, te: LinearModel(alpha=best_alpha).fit(tr).predict(te),
        "model: single":  lambda tr, te: SingleModel(**best).fit(tr).predict(te),
        "model: grouped": lambda tr, te: GroupedModel(**best).fit(tr).predict(te),
    }
    res = run_folds(df, folds, builders, only="report")
    rep = summarise(res, folds, "report")
    table(rep, "all predictors — REPORTING folds (10 months, never used for tuning)")

    # ---- the noise rule -------------------------------------------------
    print("\n  differences, block bootstrap over whole days:")
    y, idx = rep["model: single"]["_y"], rep["model: single"]["_idx"]
    comparisons = [
        ("naive: yesterday same hour", "model: single",  "single model vs best naive"),
        ("naive: yesterday same hour", "model: grouped", "grouped model vs best naive"),
        ("model: single", "model: grouped", "grouped vs single"),
        # the control: how much of the gain is the algorithm, not the features?
        ("naive: yesterday same hour", "model: ridge",   "ridge control vs best naive"),
        ("model: ridge", "model: single",  "boosting vs ridge on identical features"),
    ]
    payload["comparisons"] = {}
    for a, b, label in comparisons:
        cmp = ev.bootstrap_difference(y, rep[a]["_pred"], rep[b]["_pred"], idx)
        payload["comparisons"][label] = cmp
        print(f"    {label:32s} {cmp['diff']:+6.2f}  95% CI "
              f"[{cmp['lo']:+.2f}, {cmp['hi']:+.2f}]  {ev.verdict(cmp)}")

    winner_name = ("grouped" if payload["comparisons"]["grouped vs single"]["significant"]
                   and payload["comparisons"]["grouped vs single"]["diff"] > 0 else "single")
    winner_cls = GroupedModel if winner_name == "grouped" else SingleModel
    print(f"\n  winner by the noise rule: {winner_name}")
    payload["winner"] = winner_name

    # ---- step 5: intervals, and whether they are honest ----------------
    payload["quantile"] = step5_intervals(df, folds, winner_cls, best)

    payload["report_scores"] = {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                for k, v in rep.items()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, default=float))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    sys.exit(main())
