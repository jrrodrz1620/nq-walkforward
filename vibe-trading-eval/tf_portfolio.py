"""Phase 4: multi-instrument trend-following portfolio.

Trend-following's edge is a diversification effect, not a single-market one
(TF_RESULTS.md: NAS100 alone was lumpy — great in trends, +2% in the
2009-2011 grind-up). This runs the identical symmetric TF system (long AND
short — FX/commodities/bonds trend both ways) on 9 diversified instruments and
equal-risk-weights them on an R-multiple basis.

Each trade risks a fixed fraction of capital (RISK_FRAC). Portfolio equity is
the time-ordered accumulation of r_multiple * RISK_FRAC * capital across every
instrument's trades. Diversification = many instruments trading at different
times, so no single market's drought sinks the curve.

Benchmark: equal-weight buy-and-hold of the same 9 instruments.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import calc_metrics  # noqa: E402
from tf_strategy import TFParams, run_tf, load_ohlc_csv  # noqa: E402

CAP = 50_000.0
RISK_FRAC = 0.005          # 0.5% of capital risked per trade
BPY = 24 * 252
DATA_DIR = "/home/user/vibe-eval/data/portfolio"
INSTRUMENTS = ["NAS100_USD", "SPX500_USD", "JP225_USD", "EUR_USD", "GBP_USD",
               "XAU_USD", "WTICO_USD", "CORN_USD", "USB10Y_USD"]


def _curve_stats(equity: pd.Series) -> dict:
    ret = (equity.iloc[-1] / CAP - 1) * 100
    peak = equity.cummax()
    dd = ((equity - peak) / peak * 100).min()
    r = equity.pct_change().dropna()
    sharpe = float(r.mean() / r.std() * np.sqrt(BPY)) if r.std() > 0 else 0.0
    return {"ret": round(ret, 1), "dd": round(float(dd), 1),
            "sharpe": round(sharpe, 2), "ret_dd": round(abs(ret / dd), 2) if dd else 0.0}


def run_portfolio(params: TFParams):
    per_inst = {}
    all_trades = []
    for inst in INSTRUMENTS:
        ohlc = load_ohlc_csv(f"{DATA_DIR}/{inst}.csv")
        tr = run_tf(ohlc, params)
        if len(tr) == 0:
            continue
        tr = tr.copy()
        tr["pnl"] = tr["r_multiple"] * RISK_FRAC * CAP     # equal-risk $ per trade
        tr["inst"] = inst
        per_inst[inst] = tr
        all_trades.append(tr[["exit_time", "pnl", "r_multiple", "inst"]])

    combined = pd.concat(all_trades).sort_values("exit_time").reset_index(drop=True)
    combined["equity"] = CAP + combined["pnl"].cumsum()
    port = _curve_stats(combined.set_index("exit_time")["equity"])
    return combined, per_inst, port


def equal_weight_bh():
    rets, dds = [], []
    for inst in INSTRUMENTS:
        c = load_ohlc_csv(f"{DATA_DIR}/{inst}.csv")["close"].to_numpy(float)
        rets.append((c[-1] / c[0] - 1) * 100)
        peak = np.maximum.accumulate(c)
        dds.append(float(((c - peak) / peak * 100).min()))
    return {"ret": round(float(np.mean(rets)), 1), "dd": round(float(np.mean(dds)), 1),
            "ret_dd": round(abs(np.mean(rets) / np.mean(dds)), 2)}


if __name__ == "__main__":
    p = TFParams(allow_short=True)      # symmetric: trend both ways
    combined, per_inst, port = run_portfolio(p)

    print("=== Per-instrument (symmetric TF, 2012-2020, R-multiple) ===")
    print(f"{'instrument':<12} {'trades':>7} {'sum_R':>8} {'avg_R':>7} {'win%':>6}")
    for inst, tr in per_inst.items():
        r = tr["r_multiple"]
        print(f"{inst:<12} {len(tr):>7} {r.sum():>8.1f} {r.mean():>7.3f} {(r > 0).mean() * 100:>5.0f}%")

    bh = equal_weight_bh()
    print(f"\n=== PORTFOLIO (equal-risk {RISK_FRAC:.1%}/trade) vs equal-weight buy&hold ===")
    print(f"  PORTFOLIO:  ret {port['ret']}%  maxDD {port['dd']}%  Sharpe {port['sharpe']}  ret/DD {port['ret_dd']}")
    print(f"  EW BUY&HOLD ret {bh['ret']}%  maxDD {bh['dd']}%  ret/DD {bh['ret_dd']}")
    print(f"  total portfolio trades: {len(combined)}")

    combined.to_csv("/home/user/vibe-eval/portfolio_equity.csv", index=False)
