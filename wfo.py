"""
Walk-Forward Optimization (WFO) — the real out-of-sample test.

Unlike app.py (which walk-forward *analyzes* one fixed strategy's trade list),
this RE-OPTIMIZES parameters on each training window, then trades those chosen
parameters on the following out-of-sample window, and stitches the OOS results
into one continuous equity curve.

Anchored (expanding) design: an initial warmup window is reserved, then the
remaining bars are divided into N sequential OOS test segments. For each:
  1. Grid-search parameters on everything BEFORE the segment (train).
  2. Run the strategy with the winning params and keep only trades whose entry
     falls inside the segment (causal — uses past bars for warmup, no lookahead).

    python wfo.py                 # synthetic OHLC
    python wfo.py --csv bars.csv  # your own OHLC
    python wfo.py --folds 5

Outputs: per-fold chosen params + a stitched-OOS equity PNG (wfo_equity.png).
"""
from __future__ import annotations

import argparse
import itertools

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backtest import Params, generate_ohlc, load_ohlc_csv, run_backtest
from metrics import calc_metrics

CAPITAL = 50000.0

# Optimization grid (compact so each fold's search stays quick).
GRID = {
    "swing_length": [5, 8, 10, 14, 20],
    "rr_ratio":     [1.5, 2.0, 3.0],
    "stop_mode":    ["structure", "atr"],
}
MIN_TRAIN_TRADES = 10


def _trades_in(ohlc_upto: pd.DataFrame, params: Params,
               t0: pd.Timestamp, t1: pd.Timestamp) -> pd.DataFrame:
    """Run the strategy over `ohlc_upto` and keep trades entering in [t0, t1)."""
    trades = run_backtest(ohlc_upto, params)
    if trades.empty:
        return trades
    m = (trades["entry_time"] >= t0) & (trades["entry_time"] < t1)
    return trades[m].reset_index(drop=True)


def optimize(train_ohlc: pd.DataFrame) -> tuple[Params, dict]:
    """Pick the grid combo with the best train profit factor (min-trades guard)."""
    keys = list(GRID)
    best_params, best_pf, best_metrics = Params(), -1.0, None
    for combo in itertools.product(*GRID.values()):
        params = Params(**dict(zip(keys, combo)))
        trades = run_backtest(train_ohlc, params)
        if len(trades) < MIN_TRAIN_TRADES:
            continue
        m = calc_metrics(trades, CAPITAL)
        pf = 99.0 if m["profit_factor"] == np.inf else m["profit_factor"]
        # Prefer higher PF, break ties on net profit.
        score = (pf, m["net_profit"])
        if best_metrics is None or score > (best_pf, best_metrics["net_profit"]):
            best_params, best_pf, best_metrics = params, pf, m
    if best_metrics is None:                       # nothing cleared the guard
        best_metrics = calc_metrics(run_backtest(train_ohlc, Params()), CAPITAL)
    return best_params, best_metrics


def walk_forward(ohlc: pd.DataFrame, n_folds: int = 4,
                 warmup_frac: float = 0.4) -> dict:
    ohlc = ohlc.reset_index(drop=True)
    n = len(ohlc)
    warmup_end = int(n * warmup_frac)
    seg_edges = np.linspace(warmup_end, n, n_folds + 1, dtype=int)

    fold_rows, oos_frames = [], []
    for k in range(n_folds):
        seg_start, seg_end = seg_edges[k], seg_edges[k + 1]
        t0 = ohlc["time"].iloc[seg_start]
        t1 = ohlc["time"].iloc[seg_end - 1]

        train_ohlc = ohlc.iloc[:seg_start]
        params, train_m = optimize(train_ohlc)

        test_upto = ohlc.iloc[:seg_end]
        oos = _trades_in(test_upto, params, t0, t1)
        oos_m = calc_metrics(oos, CAPITAL)
        oos_frames.append(oos)

        fold_rows.append({
            "fold": k + 1,
            "train_end": t0.date(),
            "test_end": t1.date(),
            "swing": params.swing_length,
            "rr": params.rr_ratio,
            "stop": params.stop_mode,
            "train_pf": round(train_m["profit_factor"], 2) if train_m["profit_factor"] != np.inf else 99.0,
            "oos_trades": oos_m["n_trades"],
            "oos_pf": round(oos_m["profit_factor"], 2) if oos_m["profit_factor"] != np.inf else 99.0,
            "oos_net": round(oos_m["net_profit"], 0),
        })

    oos_all = pd.concat(oos_frames).reset_index(drop=True) if oos_frames else pd.DataFrame()
    return {"folds": pd.DataFrame(fold_rows), "oos_all": oos_all}


def plot_equity(oos_all: pd.DataFrame, folds: pd.DataFrame,
                path: str = "wfo_equity.png") -> str:
    fig, ax = plt.subplots(figsize=(9, 5))
    if not oos_all.empty:
        eq = CAPITAL + oos_all["profit_usd"].cumsum()
        ax.plot(oos_all["exit_time"], eq, color="#1f77b4", lw=1.8, label="Stitched OOS equity")
    ax.axhline(CAPITAL, color="gray", ls="--", lw=0.8)
    ax.set_title("Walk-Forward Optimization — Stitched Out-of-Sample Equity\n"
                 "(params re-chosen each fold on train, traded on the next OOS window)")
    ax.set_xlabel("Date"); ax.set_ylabel("Account Value ($)")
    ax.legend(loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="OHLC CSV (time,open,high,low,close)")
    ap.add_argument("--bars", type=int, default=6000)
    ap.add_argument("--folds", type=int, default=4)
    args = ap.parse_args()

    ohlc = load_ohlc_csv(args.csv) if args.csv else generate_ohlc(n_bars=args.bars)
    res = walk_forward(ohlc, n_folds=args.folds)

    print("── PER-FOLD (re-optimized each train window) ──")
    print(res["folds"].to_string(index=False))

    oos_all = res["oos_all"]
    if not oos_all.empty:
        m = calc_metrics(oos_all, CAPITAL)
        print("\n── STITCHED OUT-OF-SAMPLE ────────────────")
        print(f"  OOS trades    {m['n_trades']}")
        print(f"  OOS net       ${m['net_profit']:,.0f}")
        print(f"  OOS win rate  {m['win_rate']:.1f}%")
        print(f"  OOS PF        {m['profit_factor']:.2f}")
        print(f"  OOS max DD    {m['max_dd']:.1f}%")
        # Optimism gap: average train PF the optimizer chose vs realized OOS PF.
        avg_train_pf = res["folds"]["train_pf"].replace(99.0, np.nan).mean()
        print(f"\n  Avg chosen train PF {avg_train_pf:.2f}  →  realized OOS PF {m['profit_factor']:.2f}")
        print("  (a large drop here = the optimizer is curve-fitting the train windows)")

    path = plot_equity(oos_all, res["folds"])
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
