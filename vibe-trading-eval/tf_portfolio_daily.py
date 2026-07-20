"""Daily trend-following portfolio — the timeframe where TF is documented to
work. Classic Donchian 55-day breakout, wide ATR trailing stop, vol gate OFF
(trends come with expanding vol), symmetric, across 9 instruments, 2005-2020.
Equal-risk R-multiple combination. Benchmark: equal-weight buy & hold."""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/nq-walkforward")
sys.path.insert(0, "/home/user/nq-walkforward/vibe-trading-eval")
from tf_strategy import TFParams, run_tf, load_ohlc_csv

CAP = 50_000.0
RISK_FRAC = 0.005
BPY = 252
DATA_DIR = "/home/user/vibe-eval/data/portfolio_daily"
INSTRUMENTS = ["NAS100_USD", "SPX500_USD", "JP225_USD", "EUR_USD", "GBP_USD",
               "XAU_USD", "WTICO_USD", "CORN_USD", "USB10Y_USD"]


def curve_stats(equity):
    ret = (equity.iloc[-1] / CAP - 1) * 100
    peak = equity.cummax()
    dd = ((equity - peak) / peak * 100).min()
    r = equity.pct_change().dropna()
    sharpe = float(r.mean() / r.std() * np.sqrt(BPY)) if r.std() > 0 else 0.0
    return round(ret, 1), round(float(dd), 1), round(sharpe, 2), round(abs(ret / dd), 2) if dd else 0.0


def run(params, label):
    per, frames = {}, []
    for inst in INSTRUMENTS:
        ohlc = load_ohlc_csv(f"{DATA_DIR}/{inst}.csv")
        tr = run_tf(ohlc, params)
        if len(tr) == 0:
            continue
        tr = tr.copy(); tr["pnl"] = tr["r_multiple"] * RISK_FRAC * CAP; tr["inst"] = inst
        per[inst] = tr
        frames.append(tr[["exit_time", "pnl", "r_multiple", "inst"]])
    combined = pd.concat(frames).sort_values("exit_time").reset_index(drop=True)
    combined["equity"] = CAP + combined["pnl"].cumsum()
    ret, dd, sharpe, rdd = curve_stats(combined.set_index("exit_time")["equity"])
    print(f"\n=== {label} ===")
    print(f"{'instrument':<12} {'trades':>7} {'sum_R':>8} {'avg_R':>7} {'win%':>6}")
    for inst, tr in per.items():
        r = tr["r_multiple"]
        print(f"{inst:<12} {len(tr):>7} {r.sum():>8.1f} {r.mean():>7.3f} {(r > 0).mean() * 100:>5.0f}%")
    print(f"  PORTFOLIO: ret {ret}%  maxDD {dd}%  Sharpe {sharpe}  ret/DD {rdd}  ({len(combined)} trades)")
    return combined


def ew_bh():
    rets, dds = [], []
    for inst in INSTRUMENTS:
        c = load_ohlc_csv(f"{DATA_DIR}/{inst}.csv")["close"].to_numpy(float)
        rets.append((c[-1] / c[0] - 1) * 100)
        peak = np.maximum.accumulate(c); dds.append(float(((c - peak) / peak * 100).min()))
    print(f"  EW BUY&HOLD ret {np.mean(rets):.1f}%  maxDD {np.mean(dds):.1f}%  "
          f"ret/DD {abs(np.mean(rets)/np.mean(dds)):.2f}")


combined = run(TFParams(channel_len=55, trail_atr=4.0, trend_len=100,
                        use_vol_filter=False, allow_short=True), "Daily Donchian-55, gate OFF, symmetric")
ew_bh()
combined.to_csv("/home/user/vibe-eval/portfolio_daily_equity.csv", index=False)
