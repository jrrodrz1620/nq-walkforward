"""OOS confirmation of the fully-squeezed Phantom Flow on 1H: gate band
optimized per fold, risk-based sizing on, rr 2.0. Compare stitched OOS vs the
gated-but-fixed-size version (+$4.3k, PF 1.03, DD -49%)."""
import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
from backtest import Params, load_ohlc_csv, run_backtest
from metrics import calc_metrics

CAP = 50_000.0
MIN_TRAIN = 15
BANDS = {"ungated": (False, 0.0, 1.0), "20-80": (True, 0.20, 0.80),
         "30-70": (True, 0.30, 0.70), "skip-top20": (True, 0.0, 0.80)}
GRID = {"swing_length": [8, 10, 14], "band": list(BANDS)}


def _params(combo, sizing):
    gated, lo, hi = BANDS[combo["band"]]
    return Params(swing_length=combo["swing_length"], rr_ratio=2.0,
                  use_vol_filter=gated, vol_lo_pct=lo, vol_hi_pct=hi, **sizing)


def optimize(train, sizing):
    keys = list(GRID); best = best_s = None
    for vals in itertools.product(*GRID.values()):
        combo = dict(zip(keys, vals))
        tr = run_backtest(train, _params(combo, sizing))
        if len(tr) < MIN_TRAIN:
            continue
        m = calc_metrics(tr, CAP)
        pf = 99.0 if m["profit_factor"] == np.inf else m["profit_factor"]
        s = (pf, m["net_profit"])
        if best_s is None or s > best_s:
            best, best_s = combo, s
    return best or {"swing_length": 10, "band": "ungated"}


def wfo(ohlc, sizing, n_folds=4, warmup=0.4):
    ohlc = ohlc.reset_index(drop=True); n = len(ohlc)
    edges = np.linspace(int(n * warmup), n, n_folds + 1, dtype=int)
    rows, oos_frames = [], []
    for k in range(n_folds):
        s, e = edges[k], edges[k + 1]
        t0, t1 = ohlc["time"].iloc[s], ohlc["time"].iloc[e - 1]
        combo = optimize(ohlc.iloc[:s], sizing)
        tr = run_backtest(ohlc.iloc[:e], _params(combo, sizing))
        oos = tr[(tr["entry_time"] >= t0) & (tr["entry_time"] < t1)]
        oos_frames.append(oos)
        m = calc_metrics(oos, CAP) if len(oos) else None
        rows.append({"fold": k + 1, "test_end": t1.date(), "swing": combo["swing_length"],
                     "band": combo["band"], "oos_tr": len(oos),
                     "oos_net": round(m["net_profit"], 0) if m else 0,
                     "oos_pf": round(m["profit_factor"], 2) if m else 0})
    return pd.DataFrame(rows), pd.concat(oos_frames).reset_index(drop=True)


ohlc = load_ohlc_csv("/home/user/vibe-eval/data/nas100_1h_2016_2020.csv")
for label, sizing in [("fixed 2ct", dict(size_mode="fixed", contracts=2)),
                      ("risk $500", dict(size_mode="risk", risk_dollars=500))]:
    folds, oos_all = wfo(ohlc, sizing)
    print(f"\n=== 1H squeezed WFO — {label}, rr2.0, band optimized/fold ===")
    print(folds.to_string(index=False))
    m = calc_metrics(oos_all, CAP)
    print(f"  STITCHED OOS: {m['n_trades']} tr  net ${m['net_profit']:,.0f}  "
          f"PF {m['profit_factor']:.2f}  WR {m['win_rate']:.0f}%  DD {m['max_dd']:.1f}%")
