"""
Parameter sweep with walk-forward overfit visualization.

Grid-searches key Phantom Flow SMC parameters over one OHLC series. For each
combo it backtests, walk-forward splits the trades, and records in-sample (IS,
train) vs out-of-sample (OOS, test) profit factor. The point is NOT to find the
single best in-sample combo — it's to see which settings hold up OOS.

    python sweep.py                 # synthetic OHLC
    python sweep.py --csv bars.csv  # your own OHLC

Outputs:
    sweep_results.csv   full grid
    sweep_overfit.png   IS vs OOS scatter (points below the diagonal = overfit)
"""
from __future__ import annotations

import argparse
import dataclasses
import itertools

import numpy as np
import pandas as pd

from backtest import Params, generate_ohlc, load_ohlc_csv, run_backtest
from metrics import split_folds, calc_metrics

CAPITAL = 50000.0

# Grid — keep it modest so the sweep stays quick.
GRID = {
    "swing_length": [5, 8, 10, 14, 20],
    "rr_ratio":     [1.5, 2.0, 3.0],
    "stop_mode":    ["structure", "atr"],
}


def _pf(x: float) -> float:
    return 99.0 if x == np.inf else round(float(x), 2)


def run_sweep(ohlc: pd.DataFrame) -> pd.DataFrame:
    keys = list(GRID)
    rows = []
    for combo in itertools.product(*GRID.values()):
        params = Params(**dict(zip(keys, combo)))
        trades = run_backtest(ohlc, params)
        n = len(trades)
        if n < 12:
            rows.append({**dict(zip(keys, combo)), "n_trades": n,
                         "net": round(trades["profit_usd"].sum(), 0) if n else 0,
                         "is_pf": np.nan, "oos_pf": np.nan, "gap": np.nan})
            continue
        folds = split_folds(trades, 5, 70, 3)
        if not folds:
            rows.append({**dict(zip(keys, combo)), "n_trades": n,
                         "net": round(trades["profit_usd"].sum(), 0),
                         "is_pf": np.nan, "oos_pf": np.nan, "gap": np.nan})
            continue
        is_pf = _pf(calc_metrics(pd.concat([f["train"] for f in folds]), CAPITAL)["profit_factor"])
        oos_pf = _pf(calc_metrics(pd.concat([f["test"] for f in folds]), CAPITAL)["profit_factor"])
        rows.append({**dict(zip(keys, combo)), "n_trades": n,
                     "net": round(trades["profit_usd"].sum(), 0),
                     "is_pf": is_pf, "oos_pf": oos_pf, "gap": round(is_pf - oos_pf, 2)})
    return pd.DataFrame(rows)


def plot_overfit(df: pd.DataFrame, path: str = "sweep_overfit.png") -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = df.dropna(subset=["is_pf", "oos_pf"]).copy()
    fig, ax = plt.subplots(figsize=(7, 6))
    sc = ax.scatter(d["is_pf"], d["oos_pf"], c=d["n_trades"], cmap="viridis",
                    s=70, edgecolor="white", linewidth=0.5, zorder=3)
    lim = [0, max(2.0, d["is_pf"].max(), d["oos_pf"].max()) * 1.05]
    ax.plot(lim, lim, "--", color="gray", label="IS = OOS (no degradation)", zorder=1)
    ax.axhline(1.0, color="red", lw=0.8, alpha=0.6, zorder=1)
    ax.axvline(1.0, color="red", lw=0.8, alpha=0.6, zorder=1)
    ax.fill_between(lim, 1.0, lim, where=[v >= 1 for v in lim], color="green", alpha=0.05, zorder=0)
    ax.set_xlim(lim); ax.set_ylim(0, lim[1])
    ax.set_xlabel("In-Sample Profit Factor (train)")
    ax.set_ylabel("Out-of-Sample Profit Factor (test)")
    ax.set_title("Parameter Sweep — Overfit Map\nPoints below the diagonal degrade out-of-sample")
    fig.colorbar(sc, label="# trades")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="OHLC CSV (time,open,high,low,close)")
    ap.add_argument("--bars", type=int, default=4000)
    args = ap.parse_args()

    ohlc = load_ohlc_csv(args.csv) if args.csv else generate_ohlc(n_bars=args.bars)
    print(f"Sweeping {np.prod([len(v) for v in GRID.values()])} combos...\n")

    df = run_sweep(ohlc)
    df.to_csv("sweep_results.csv", index=False)

    ranked = df.dropna(subset=["oos_pf"]).sort_values("oos_pf", ascending=False)
    print("── TOP BY OUT-OF-SAMPLE PF ───────────────")
    print(ranked.head(10).to_string(index=False))
    print("\n── MOST OVERFIT (largest IS→OOS drop) ────")
    print(df.dropna(subset=["gap"]).sort_values("gap", ascending=False)
            .head(5).to_string(index=False))

    path = plot_overfit(df)
    print(f"\nWrote sweep_results.csv and {path}")


if __name__ == "__main__":
    main()
