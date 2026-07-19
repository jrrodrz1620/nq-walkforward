"""Honest OOS test of the volatility gate: walk-forward optimization where the
ATR band is one of the optimized parameters.

If the gate is real (not an in-sample fluke), the optimizer should (a) keep
choosing gated bands each train window rather than 'ungated', and (b) produce
a positive stitched out-of-sample equity. If it's noise, chosen bands will
scatter and OOS will still lose.
"""
import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
from backtest import Params, load_ohlc_csv, run_backtest
from metrics import calc_metrics

CAP = 50_000.0
MIN_TRAIN_TRADES = 15

# Band choices the optimizer may pick, including ungated as the null.
BANDS = {
    "ungated": (False, 0.0, 1.0),
    "20-80":   (True, 0.20, 0.80),
    "30-70":   (True, 0.30, 0.70),
    "skip-top20": (True, 0.0, 0.80),
    "lo<50":   (True, 0.0, 0.50),
}
GRID = {
    "swing_length": [8, 10, 14],
    "rr_ratio": [1.5, 2.0, 3.0],
    "band": list(BANDS),
}


def _params(combo: dict) -> Params:
    gated, lo, hi = BANDS[combo["band"]]
    return Params(swing_length=combo["swing_length"], rr_ratio=combo["rr_ratio"],
                  use_vol_filter=gated, vol_lo_pct=lo, vol_hi_pct=hi)


def optimize(train_ohlc: pd.DataFrame):
    keys = list(GRID)
    best, best_score = None, None
    for values in itertools.product(*GRID.values()):
        combo = dict(zip(keys, values))
        trades = run_backtest(train_ohlc, _params(combo))
        if len(trades) < MIN_TRAIN_TRADES:
            continue
        m = calc_metrics(trades, CAP)
        pf = 99.0 if m["profit_factor"] == np.inf else m["profit_factor"]
        score = (pf, m["net_profit"])
        if best_score is None or score > best_score:
            best, best_score = combo, score
    return best or {"swing_length": 10, "rr_ratio": 2.0, "band": "ungated"}


def walk_forward(ohlc: pd.DataFrame, n_folds=4, warmup_frac=0.4):
    ohlc = ohlc.reset_index(drop=True)
    n = len(ohlc)
    edges = np.linspace(int(n * warmup_frac), n, n_folds + 1, dtype=int)
    rows, oos_frames = [], []
    for k in range(n_folds):
        s, e = edges[k], edges[k + 1]
        t0, t1 = ohlc["time"].iloc[s], ohlc["time"].iloc[e - 1]
        combo = optimize(ohlc.iloc[:s])
        trades = run_backtest(ohlc.iloc[:e], _params(combo))
        oos = trades[(trades["entry_time"] >= t0) & (trades["entry_time"] < t1)]
        m = calc_metrics(oos, CAP) if len(oos) else None
        oos_frames.append(oos)
        rows.append({
            "fold": k + 1, "test_end": t1.date(),
            "swing": combo["swing_length"], "rr": combo["rr_ratio"],
            "band": combo["band"],
            "oos_trades": len(oos),
            "oos_net": round(m["net_profit"], 0) if m else 0,
            "oos_pf": round(m["profit_factor"], 2) if m else 0,
        })
    oos_all = pd.concat(oos_frames).reset_index(drop=True)
    return pd.DataFrame(rows), oos_all


for name, path in [("1H 2016-2020", "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv"),
                   ("15m 2016-2018", "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv"),
                   ("15m 2019-2020", "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv")]:
    folds, oos_all = walk_forward(load_ohlc_csv(path))
    print(f"\n=== {name} — gated WFO (band is optimized per fold) ===")
    print(folds.to_string(index=False))
    if len(oos_all) >= 5:
        m = calc_metrics(oos_all, CAP)
        print(f"  STITCHED OOS: {m['n_trades']} trades  net ${m['net_profit']:,.0f}  "
              f"PF {m['profit_factor']:.2f}  WR {m['win_rate']:.0f}%  maxDD {m['max_dd']:.1f}%")
