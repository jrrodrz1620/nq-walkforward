"""Run the full gauntlet on the mean-reversion strategy: significance tests
in-sample, then walk-forward optimization (entry_z x stop_atr per fold)."""
import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
sys.path.insert(0, "/home/user/nq-walkforward/vibe-trading-eval")
from metrics import calc_metrics, permutation_test, bootstrap_sharpe_ci
from mr_strategy import MRParams, run_mr, load_ohlc_csv

CAP = 50_000.0
DATASETS = {
    "1H full": "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv",
    "15m calm": "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv",
    "15m vol": "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv",
}
GRID = {"entry_z": [1.5, 2.0, 2.5], "stop_atr": [1.0, 1.5, 2.0]}
MIN_TRAIN = 20


def optimize(train):
    keys = list(GRID); best = best_s = None
    for vals in itertools.product(*GRID.values()):
        combo = dict(zip(keys, vals))
        tr = run_mr(train, MRParams(trend_mode="with", **combo))
        if len(tr) < MIN_TRAIN:
            continue
        m = calc_metrics(tr, CAP)
        pf = 99.0 if m["profit_factor"] == np.inf else m["profit_factor"]
        s = (pf, m["net_profit"])
        if best_s is None or s > best_s:
            best, best_s = combo, s
    return best or {"entry_z": 2.0, "stop_atr": 1.5}


def wfo(ohlc, n_folds=4, warmup=0.4):
    ohlc = ohlc.reset_index(drop=True); n = len(ohlc)
    edges = np.linspace(int(n * warmup), n, n_folds + 1, dtype=int)
    rows, frames = [], []
    for k in range(n_folds):
        s, e = edges[k], edges[k + 1]
        t0, t1 = ohlc["time"].iloc[s], ohlc["time"].iloc[e - 1]
        combo = optimize(ohlc.iloc[:s])
        tr = run_mr(ohlc.iloc[:e], MRParams(trend_mode="with", **combo))
        oos = tr[(tr["entry_time"] >= t0) & (tr["entry_time"] < t1)]
        frames.append(oos)
        m = calc_metrics(oos, CAP) if len(oos) else None
        rows.append({"fold": k + 1, "test_end": t1.date(), "entry_z": combo["entry_z"],
                     "stop_atr": combo["stop_atr"], "oos_tr": len(oos),
                     "oos_net": round(m["net_profit"], 0) if m else 0,
                     "oos_pf": round(m["profit_factor"], 2) if m else 0})
    return pd.DataFrame(rows), pd.concat(frames).reset_index(drop=True)


for name, path in DATASETS.items():
    ohlc = load_ohlc_csv(path)
    tr = run_mr(ohlc, MRParams(trend_mode="with"))
    pnl = tr["profit_usd"].to_numpy()
    perm = permutation_test(pnl, CAP, n_sims=2000)
    ci = bootstrap_sharpe_ci(pnl, n_boot=2000)
    print(f"\n=== {name} — MR with-trend ===")
    print(f"  in-sample: {len(tr)} tr  permutation maxDD p={perm['p_value_maxdd']:.2f}  "
          f"Sharpe CI [{ci['ci_lower']:.2f},{ci['ci_upper']:.2f}]  P(Sharpe>0) {ci['prob_positive']:.0%}")
    folds, oos_all = wfo(ohlc)
    print(folds.to_string(index=False))
    m = calc_metrics(oos_all, CAP)
    print(f"  STITCHED OOS: {m['n_trades']} tr  net ${m['net_profit']:,.0f}  "
          f"PF {m['profit_factor']:.2f}  WR {m['win_rate']:.0f}%  DD {m['max_dd']:.1f}%")
