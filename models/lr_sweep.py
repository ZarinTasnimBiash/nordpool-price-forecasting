"""Does the learning rate actually matter? A one-factor sweep.

Run:  PYTHONPATH=. OMP_NUM_THREADS=1 python -u models/lr_sweep.py

The Stage 5 tuning grid varies several settings at once, so it cannot say
whether the learning rate matters on its own: the two configs that were not
0.05 each changed `min_samples_leaf` at the same time. That is a confound, and
"0.05 won" is evidence about a *config*, not about a learning rate.

This holds everything else fixed at the chosen config and moves only the one
knob, on the same five tuning folds — the reporting folds stay untouched.
"""

from __future__ import annotations

import numpy as np

from models import evaluation as ev
from models.predictors import TARGET, SingleModel
from models.run_stage5 import load_features

RATES = (0.02, 0.03, 0.05, 0.08, 0.12, 0.20)
FIXED = dict(max_leaf_nodes=15, min_samples_leaf=80)
DEFAULT_LR = 0.05          # what ships; everything is compared against it


def main() -> None:
    df = load_features()
    tune = [f for f in ev.make_folds(df.index) if f["purpose"] == "tune"]
    print(f"one-factor sweep over {len(tune)} tuning folds; fixed: {FIXED}\n")
    print(f"{'lr':>6s} {'MAE':>7s} {'fold sd':>8s} {'trees':>7s}   per-fold MAE")

    pooled = {}
    for lr in RATES:
        maes, trees, P, Y, IDX = [], [], [], [], []
        for f in tune:
            tr, te = df[f["train"]], df[f["test"]]
            m = SingleModel(learning_rate=lr, **FIXED).fit(tr)
            pred = m.predict(te)
            maes.append(float(np.abs(pred - te[TARGET].to_numpy()).mean()))
            trees.append(m.n_trees_)
            P.append(pred); Y.append(te[TARGET].to_numpy(float)); IDX.append(te.index)
        pooled[lr] = (np.concatenate(P), np.concatenate(Y), IDX[0].append(IDX[1:]))
        print(f"{lr:6.2f} {np.mean(maes):7.2f} {np.std(maes):8.2f} {int(np.mean(trees)):7d}   "
              + " ".join(f"{x:.1f}" for x in maes))

    # A ranking is not a result. Every rate is compared against the shipped one
    # under the project's noise rule, on the same days — a paired comparison, so
    # the shared "some months are just harder" variance cancels and the test is
    # far more sensitive than the fold sd column suggests.
    _, y, idx = pooled[DEFAULT_LR][0], pooled[DEFAULT_LR][1], pooled[DEFAULT_LR][2]
    print(f"\nagainst lr={DEFAULT_LR}, block bootstrap over whole days:")
    for lr in RATES:
        if lr == DEFAULT_LR:
            continue
        c = ev.bootstrap_difference(y, pooled[DEFAULT_LR][0], pooled[lr][0], idx)
        print(f"  lr {lr:<5} {c['diff']:+.3f}  95% CI [{c['lo']:+.3f}, {c['hi']:+.3f}]  "
              f"{ev.verdict(c)}")


if __name__ == "__main__":
    main()
