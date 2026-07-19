"""Gauntlet on the long-only trend-follower: significance + WFO, each dataset
compared to its buy-and-hold benchmark (return/DD is the bar to clear)."""
import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
sys.path.insert(0, "/home/user/nq-walkforward/vibe-trading-eval")
from metrics import calc_metrics, permutation_test, bootstrap_sharpe_ci
from tf_strategy import TFParams, run_tf, buy_and_hold_metrics, load_ohlc_csv

CAP = 50_000.0
DATA = [("1H full", "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv", 24 * 252),
        ("15m calm", "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv", 96 * 252),
        ("15m vol", "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv", 96 * 252)]
GRID = {"channel_len": [20, 40, 60], "trail_atr": [2.0, 3.0, 4.0]}
MIN_TRAIN = 20


def optimize(train):
    keys = list(GRID); best = best_s = None
    for vals in itertools.product(*GRID.values()):
        combo = dict(zip(keys, vals))
        tr = run_tf(train, TFParams(allow_short=False, **combo))
        if len(tr) < MIN_TRAIN:
            continue
        m = calc_metrics(tr, CAP)
        pf = 99.0 if m["profit_factor"] == np.inf else m["profit_factor"]
        s = (pf, m["net_profit"])
        if best_s is None or s > best_s:
            best, best_s = combo, s
    return best or {"channel_len": 40, "trail_atr": 3.0}


def wfo(ohlc, n_folds=4, warmup=0.4):
    ohlc = ohlc.reset_index(drop=True); n = len(ohlc)
    edges = np.linspace(int(n * warmup), n, n_folds + 1, dtype=int)
    frames = []
    for k in range(n_folds):
        s, e = edges[k], edges[k + 1]
        t0, t1 = ohlc["time"].iloc[s], ohlc["time"].iloc[e - 1]
        combo = optimize(ohlc.iloc[:s])
        tr = run_tf(ohlc.iloc[:e], TFParams(allow_short=False, **combo))
        frames.append(tr[(tr["entry_time"] >= t0) & (tr["entry_time"] < t1)])
    return pd.concat(frames).reset_index(drop=True)


for name, path, bpy in DATA:
    ohlc = load_ohlc_csv(path)
    bh = buy_and_hold_metrics(ohlc, bpy)
    tr = run_tf(ohlc, TFParams(allow_short=False))
    pnl = tr["profit_usd"].to_numpy()
    ci = bootstrap_sharpe_ci(pnl, n_boot=2000)
    perm = permutation_test(pnl, CAP, n_sims=2000)
    m_is = calc_metrics(tr, CAP)
    oos = wfo(ohlc)
    m = calc_metrics(oos, CAP)
    print(f"\n=== {name} — long-only trend-follower ===")
    print(f"  BUY&HOLD  return {bh['total_return_pct']}%  DD {bh['max_dd_pct']}%  ret/DD {bh['ret_over_dd']}")
    print(f"  in-sample ret {m_is['return_pct']:.1f}%  DD {m_is['max_dd']:.1f}%  "
          f"P(Sharpe>0) {ci['prob_positive']:.0%}  perm maxDD p={perm['p_value_maxdd']:.2f}")
    print(f"  WFO OOS   {m['n_trades']} tr  net ${m['net_profit']:,.0f}  PF {m['profit_factor']:.2f}  "
          f"ret {m['return_pct']:.1f}%  DD {m['max_dd']:.1f}%")
