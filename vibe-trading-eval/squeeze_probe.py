"""Phase 1 in-sample probe: gate ON, sweep target geometry x sizing.

Fixes the two known weaknesses of the gated Phantom Flow — position sizing
(-40 to -49% drawdowns) and target geometry (41-43% WR can't pay for 2R/3R).
Gate held at the robust mid-band 20-80 throughout.
"""
import sys

import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
from backtest import Params, load_ohlc_csv, run_backtest
from metrics import calc_metrics

CAP = 50_000.0
DATASETS = {
    "1H full": "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv",
    "15m calm": "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv",
    "15m vol": "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv",
}
RRS = [1.0, 1.25, 1.5, 2.0]
SIZINGS = [("fixed 2ct", dict(size_mode="fixed", contracts=2)),
           ("risk $500", dict(size_mode="risk", risk_dollars=500)),
           ("risk $750", dict(size_mode="risk", risk_dollars=750))]


def line(trades):
    if len(trades) < 5:
        return f"{len(trades):>4} tr (too few)"
    m = calc_metrics(trades, CAP)
    return (f"{m['n_trades']:>4} tr  net ${m['net_profit']:>8,.0f}  PF {m['profit_factor']:.2f}  "
            f"WR {m['win_rate']:.0f}%  DD {m['max_dd']:>6.1f}%")


for name, path in DATASETS.items():
    ohlc = load_ohlc_csv(path)
    print(f"\n=== {name}  (gate mid-band 20-80) ===")
    for szlabel, sz in SIZINGS:
        for rr in RRS:
            p = Params(use_vol_filter=True, vol_lo_pct=0.20, vol_hi_pct=0.80,
                       rr_ratio=rr, **sz)
            print(f"  {szlabel:<10} rr {rr:<4}  {line(run_backtest(ohlc, p))}")
