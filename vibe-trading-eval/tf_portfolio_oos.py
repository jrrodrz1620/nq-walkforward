"""OOS confirmation + equity plot for the daily trend portfolio.
Optimize (channel_len, trail_atr) for best portfolio Sharpe on 2005-2012,
apply UNCHANGED to 2013-2020. Plot both-era portfolio equity vs buy & hold."""
import itertools
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
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
SPLIT = pd.Timestamp("2013-01-01")
GRID = {"channel_len": [20, 40, 55, 80, 100], "trail_atr": [2.0, 3.0, 4.0]}

OHLC = {i: load_ohlc_csv(f"{DATA_DIR}/{i}.csv") for i in INSTRUMENTS}


def portfolio_trades(params, lo=None, hi=None):
    frames = []
    for inst, ohlc in OHLC.items():
        tr = run_tf(ohlc, params)
        if len(tr) == 0:
            continue
        tr = tr.copy()
        if lo is not None:
            tr = tr[tr["entry_time"] >= lo]
        if hi is not None:
            tr = tr[tr["entry_time"] < hi]
        if len(tr):
            tr["pnl"] = tr["r_multiple"] * RISK_FRAC * CAP
            frames.append(tr[["exit_time", "pnl"]])
    if not frames:
        return pd.DataFrame(columns=["exit_time", "pnl"])
    return pd.concat(frames).sort_values("exit_time").reset_index(drop=True)


def stats(trades):
    if len(trades) < 5:
        return None
    eq = CAP + trades.set_index("exit_time")["pnl"].cumsum()
    ret = (eq.iloc[-1] / CAP - 1) * 100
    dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    r = eq.pct_change().dropna()
    sharpe = float(r.mean() / r.std() * np.sqrt(BPY)) if r.std() > 0 else 0.0
    return {"ret": round(ret, 1), "dd": round(float(dd), 1), "sharpe": round(sharpe, 2),
            "ret_dd": round(abs(ret / dd), 2) if dd else 0.0, "eq": eq}


# --- optimize on 2005-2012 ---
best, best_sharpe = None, -9
for cl, ta in itertools.product(*GRID.values()):
    s = stats(portfolio_trades(TFParams(channel_len=cl, trail_atr=ta, trend_len=100,
                                        use_vol_filter=False, allow_short=True), hi=SPLIT))
    if s and s["sharpe"] > best_sharpe:
        best, best_sharpe = (cl, ta), s["sharpe"]
cl, ta = best
print(f"Best on 2005-2012 (in-sample): channel_len={cl}, trail_atr={ta}  Sharpe {best_sharpe}")

params = TFParams(channel_len=cl, trail_atr=ta, trend_len=100, use_vol_filter=False, allow_short=True)
is_s = stats(portfolio_trades(params, hi=SPLIT))
oos_s = stats(portfolio_trades(params, lo=SPLIT))
print(f"  IN-SAMPLE 2005-2012:  ret {is_s['ret']}%  DD {is_s['dd']}%  Sharpe {is_s['sharpe']}  ret/DD {is_s['ret_dd']}")
print(f"  OUT-SAMPLE 2013-2020: ret {oos_s['ret']}%  DD {oos_s['dd']}%  Sharpe {oos_s['sharpe']}  ret/DD {oos_s['ret_dd']}")

# EW buy & hold on OOS window
rets, dds = [], []
for inst, ohlc in OHLC.items():
    o = ohlc[ohlc["time"] >= SPLIT]
    c = o["close"].to_numpy(float)
    rets.append((c[-1] / c[0] - 1) * 100)
    peak = np.maximum.accumulate(c); dds.append(float(((c - peak) / peak * 100).min()))
print(f"  OOS EW BUY&HOLD:      ret {np.mean(rets):.1f}%  DD {np.mean(dds):.1f}%  ret/DD {abs(np.mean(rets)/np.mean(dds)):.2f}")

# --- plot full-period equity ---
full = portfolio_trades(params)
eq = CAP + full.set_index("exit_time")["pnl"].cumsum()
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(eq.index, eq.values, color="#1f77b4", lw=1.6, label="Trend portfolio (9 instruments, equal-risk)")
ax.axvline(SPLIT, color="gray", ls="--", lw=1, label="train/test split")
ax.axhline(CAP, color="black", lw=0.6, alpha=0.4)
ax.set_title("Daily trend-following portfolio — equity (params fixed on 2005-2012, applied through 2020)")
ax.set_xlabel("Date"); ax.set_ylabel("Account value ($)")
ax.legend(loc="upper left"); fig.tight_layout()
fig.savefig("/home/user/vibe-eval/portfolio_equity.png", dpi=120)
print("\nWrote portfolio_equity.png")
