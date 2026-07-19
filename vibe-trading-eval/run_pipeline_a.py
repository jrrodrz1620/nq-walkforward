"""Pipeline A: nq-walkforward's own stack on the shared NQ dataset.

Two variants:
  A1 "defaults"      — Params() as shipped (partial at 1R, $2.00 commission)
  A2 "like-for-like" — partial off, $2.25/side commission to match the
                       Vibe-Trading GlobalFuturesEngine NQ defaults
"""
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
from backtest import Params, load_ohlc_csv, run_backtest  # noqa: E402
from metrics import calc_metrics, monte_carlo, split_folds  # noqa: E402

CAPITAL = 72_000.0  # matches Vibe-Trading run's initial_cash


def analyze(trades: pd.DataFrame, label: str) -> dict:
    out = {"label": label, "n_trades": int(len(trades))}
    if len(trades) < 10:
        return out
    m = calc_metrics(trades, CAPITAL)
    overall = {}
    for k, v in m.items():
        if isinstance(v, (int, float, np.integer, np.floating)):
            overall[k] = None if not np.isfinite(v) else round(float(v), 4)
    out["overall"] = overall
    folds = split_folds(trades, 5, 70, 5)
    if folds:
        oos = calc_metrics(pd.concat([f["test"] for f in folds]), CAPITAL)
        tr = calc_metrics(pd.concat([f["train"] for f in folds]), CAPITAL)
        ratio = oos["profit_factor"] / tr["profit_factor"] if tr["profit_factor"] > 0 else 0.0
        out["walk_forward"] = {
            "folds": len(folds),
            "oos_trades": int(oos["n_trades"]),
            "train_pf": round(float(tr["profit_factor"]), 3),
            "oos_pf": round(float(oos["profit_factor"]), 3),
            "oos_net": round(float(oos["net_profit"]), 2),
            "ratio": round(float(ratio), 3),
        }
    mc = monte_carlo(trades["profit_usd"].to_numpy(), CAPITAL, n_sims=2000)
    out["monte_carlo"] = {
        k: round(float(v), 2)
        for k, v in mc.items()
        if isinstance(v, (int, float, np.integer, np.floating))
    }
    return out


ohlc = load_ohlc_csv("/home/user/vibe-eval/data/nq_bars.csv")

a1 = analyze(run_backtest(ohlc, Params(commission=2.0)), "A1 defaults (partial on)")
a2 = analyze(
    run_backtest(ohlc, Params(use_partial=False, commission=2.25)),
    "A2 like-for-like (partial off, $2.25 comm)",
)

result = {"bars": int(len(ohlc)), "variants": [a1, a2]}
with open("/home/user/vibe-eval/pipeline_a_results.json", "w") as f:
    json.dump(result, f, indent=2, default=str)
print(json.dumps(result, indent=2, default=str))
