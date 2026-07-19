"""Confirm-or-falsify the 1H long-only trend-follower across independent
decades. Each ~4y window: WFO OOS vs buy-and-hold over the SAME OOS window.
If TF beats B&H risk-adjusted only on 2016-2020, it was a favorable-window
artifact. If it holds across 2008-2011 (crisis) and 2012-2015 too, it's the
real, regime-dependent trend-following edge."""
import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
sys.path.insert(0, "/home/user/nq-walkforward/vibe-trading-eval")
from metrics import calc_metrics
from tf_strategy import TFParams, run_tf, buy_and_hold_metrics, load_ohlc_csv

CAP = 50_000.0
BPY = 24 * 252
WINDOWS = [
    ("1H 2008-2011 (crisis)", "/home/user/vibe-eval/data/nas100_1h_2008_2011.csv"),
    ("1H 2012-2015 (recovery)", "/home/user/vibe-eval/data/nas100_1h_2012_2015.csv"),
    ("1H 2016-2020 (already tested)", "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv"),
]
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
    oos_start = ohlc["time"].iloc[int(n * warmup)]
    return pd.concat(frames).reset_index(drop=True), oos_start


print(f"{'window':<32} {'TF ret/DD':>10} {'B&H ret/DD':>11}  verdict")
for name, path in WINDOWS:
    ohlc = load_ohlc_csv(path)
    oos_trades, oos_start = wfo(ohlc)
    m = calc_metrics(oos_trades, CAP)
    oos_bh = ohlc[ohlc["time"] >= oos_start].reset_index(drop=True)
    bh = buy_and_hold_metrics(oos_bh, BPY)
    tf_rdd = abs(m["return_pct"] / m["max_dd"]) if m["max_dd"] else 0.0
    verdict = "TF wins" if tf_rdd > bh["ret_over_dd"] else "B&H wins"
    print(f"{name:<32} {tf_rdd:>10.2f} {bh['ret_over_dd']:>11.2f}  {verdict}  "
          f"(TF ret {m['return_pct']:.0f}%/DD {m['max_dd']:.0f}%  "
          f"B&H {bh['total_return_pct']:.0f}%/{bh['max_dd_pct']:.0f}%)")
