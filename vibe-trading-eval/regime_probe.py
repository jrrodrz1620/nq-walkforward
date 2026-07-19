"""Fail-fast probe: does a volatility regime gate rescue Phantom Flow?

Runs the ungated baseline, then a grid of ATR-percentile bands, on each real
dataset. Reports net / PF / trades / maxDD so we can see immediately whether
ANY gate turns the strategy profitable before spending time on sweep+WFO.
"""
import sys

import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
from backtest import Params, load_ohlc_csv, run_backtest
from metrics import calc_metrics

CAP = 50_000.0
DATASETS = {
    "15m 2019-2020 (volatile)": "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv",
    "15m 2016-2018 (calm)": "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv",
    "1H 2016-2020 (full)": "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv",
}
# (label, lo_pct, hi_pct)
BANDS = [
    ("ungated", 0.0, 1.0),
    ("skip top 20% vol", 0.0, 0.80),
    ("skip top 40% vol", 0.0, 0.60),
    ("mid band 20-80", 0.20, 0.80),
    ("mid band 30-70", 0.30, 0.70),
    ("only low vol <50", 0.0, 0.50),
    ("only high vol >50", 0.50, 1.0),
]


def row(trades: pd.DataFrame) -> str:
    if len(trades) < 5:
        return f"{len(trades):>4} trades  (too few)"
    m = calc_metrics(trades, CAP)
    return (f"{m['n_trades']:>4} tr   net ${m['net_profit']:>9,.0f}   "
            f"PF {m['profit_factor']:.2f}   WR {m['win_rate']:.0f}%   "
            f"maxDD {m['max_dd']:>6.1f}%")


for name, path in DATASETS.items():
    ohlc = load_ohlc_csv(path)
    print(f"\n=== {name} ===")
    for label, lo, hi in BANDS:
        gated = label != "ungated"
        p = Params(use_vol_filter=gated, vol_lo_pct=lo, vol_hi_pct=hi)
        print(f"  {label:<20} {row(run_backtest(ohlc, p))}")
