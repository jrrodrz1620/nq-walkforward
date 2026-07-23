# /// script
# requires-python = ">=3.10"
# dependencies = ["arch>=6.0", "pandas>=2.0", "numpy>=1.24", "matplotlib>=3.7"]
# ///
"""
compare.py — the honest test. Same strategy, two position-sizing rules.

Runs any signal series twice:
  A) FIXED SIZE   — every trade at 1x
  B) VOL-TARGETED — every trade sized by the GARCH forecast (target_vol / forecast_vol)

Same entries. Same exits. The ONLY difference is how much.
Then it shows you both equity curves and the numbers, side by side.

Built-in demo strategy: EMA 9/21 crossover, long/flat.
Bring your own: --signals my_signals.csv (columns: date, signal in {-1, 0, 1})

Usage:
  uv run compare.py --csv prices.csv                       # EMA 9/21 demo
  uv run compare.py --csv prices.csv --target-vol 15
  uv run compare.py --csv prices.csv --signals mine.csv    # your own strategy
  uv run compare.py --csv prices.csv --chart equity.png
"""

import argparse
import json

import numpy as np
import pandas as pd

from garch_forecast import load_prices, walkforward_garch, HONESTY_NOTE
from vol_target import size_series


# ---------------------------------------------------------------- strategies
def ema_crossover_signals(close: pd.Series, fast: int = 9, slow: int = 21) -> pd.Series:
    """EMA fast/slow crossover, long/flat. Signal known at close of day t."""
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    return (ema_f > ema_s).astype(float)  # 1 = long, 0 = flat


def load_signals(path: str, dates: pd.Series) -> pd.Series:
    df = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in df.columns}
    df = df.rename(columns={cols["date"]: "date", cols["signal"]: "signal"})
    df["date"] = pd.to_datetime(df["date"])
    merged = pd.DataFrame({"date": dates}).merge(df[["date", "signal"]], on="date", how="left")
    return merged["signal"].ffill().fillna(0.0).clip(-1, 1)


# ------------------------------------------------------------------- metrics
def perf_stats(daily_ret: pd.Series, periods_per_year: int) -> dict:
    r = daily_ret.dropna() / 100.0
    if len(r) == 0:
        return {}
    equity = (1 + r).cumprod()
    yrs = len(r) / periods_per_year
    cagr = equity.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    ann_vol = r.std() * np.sqrt(periods_per_year)
    sharpe = (r.mean() * periods_per_year) / ann_vol if ann_vol > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    return {
        "CAGR_pct": round(100 * cagr, 1),
        "ann_vol_pct": round(100 * ann_vol, 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(100 * dd, 1),
        "final_equity_x": round(float(equity.iloc[-1]), 2),
    }


def worst_month(daily_ret: pd.Series, dates: pd.Series) -> float:
    r = pd.Series(daily_ret.values / 100.0, index=pd.to_datetime(dates.values))
    m = r.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    return round(100 * m.min(), 1)


# ------------------------------------------------------------------ backtest
def run_comparison(prices: pd.DataFrame, signals: pd.Series = None,
                   target_vol: float = 15.0, periods_per_year: int = 365):
    """
    Timing discipline (zero lookahead):
      signal known at close of t  ->  applied to return of t+1
      vol forecast made at close of t (for t+1)  ->  sizes the t+1 position
    """
    wf = walkforward_garch(prices, periods_per_year=periods_per_year)
    close = wf["close"]

    sig = ema_crossover_signals(close) if signals is None else signals.reset_index(drop=True)

    next_ret = wf["ret"].shift(-1)                       # return of day t+1
    mult = size_series(wf["fcast_vol_ann"], target_vol)  # forecast made at t, for t+1

    strat_fixed = sig * next_ret
    strat_volt = sig * mult * next_ret

    valid = wf["fcast_vol"].notna() & next_ret.notna()
    dates = wf.loc[valid, "date"]
    fixed = strat_fixed[valid]
    volt = strat_volt[valid]

    stats = {
        "fixed_size": {**perf_stats(fixed, periods_per_year),
                       "worst_month_pct": worst_month(fixed, dates)},
        "vol_targeted": {**perf_stats(volt, periods_per_year),
                         "worst_month_pct": worst_month(volt, dates)},
    }
    curves = pd.DataFrame({
        "date": dates.values,
        "fixed": (1 + fixed.values / 100).cumprod(),
        "vol_targeted": (1 + volt.values / 100).cumprod(),
        "regime": wf.loc[valid, "regime"].values,
    })
    return stats, curves


def plot_curves(curves: pd.DataFrame, out_path: str, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6.5), facecolor="white")
    d = pd.to_datetime(curves["date"])
    ax.plot(d, curves["fixed"], lw=1.6, color="#888888", label="Fixed size (1x every trade)")
    ax.plot(d, curves["vol_targeted"], lw=1.8, color="#0a7d38", label="Vol-targeted (GARCH sized)")

    storm = (curves["regime"] == "storm").to_numpy()
    ax.fill_between(d, 0, 1, where=storm, transform=ax.get_xaxis_transform(),
                    color="#d62728", alpha=0.07, label="Storm regime")

    ax.set_yscale("log")
    ax.set_ylabel("Growth of $1 (log scale)")
    ax.set_title(title, fontsize=13, fontweight="bold", loc="left")
    ax.legend(frameon=False, loc="upper left")
    ax.grid(alpha=0.25, lw=0.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--ticker")
    ap.add_argument("--signals", help="CSV with date,signal columns for your own strategy")
    ap.add_argument("--target-vol", type=float, default=15.0)
    ap.add_argument("--periods-per-year", type=int, default=365)
    ap.add_argument("--chart", default="equity_comparison.png")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    prices = load_prices(csv=args.csv, ticker=args.ticker)
    sig = None
    strategy_name = "EMA 9/21 crossover (long/flat)"
    if args.signals:
        wf_dates = prices["date"].iloc[1:].reset_index(drop=True)
        sig = load_signals(args.signals, wf_dates)
        strategy_name = f"custom signals ({args.signals})"

    stats, curves = run_comparison(prices, signals=sig,
                                   target_vol=args.target_vol,
                                   periods_per_year=args.periods_per_year)
    chart = plot_curves(curves, args.chart,
                        f"Same strategy, two sizing rules — {strategy_name}")

    payload = {"strategy": strategy_name, "target_vol_pct": args.target_vol,
               "results": stats, "chart": chart, "note": HONESTY_NOTE}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\n  {strategy_name} — fixed vs vol-targeted (target {args.target_vol}%)\n")
        print(f"  {'':22}{'FIXED':>10}{'VOL-TARGETED':>15}")
        rows = [("CAGR %", "CAGR_pct"), ("Ann vol %", "ann_vol_pct"), ("Sharpe", "sharpe"),
                ("Max drawdown %", "max_drawdown_pct"), ("Worst month %", "worst_month_pct"),
                ("Final equity (x)", "final_equity_x")]
        for label, key in rows:
            print(f"  {label:22}{stats['fixed_size'][key]:>10}{stats['vol_targeted'][key]:>15}")
        print(f"\n  chart: {chart}")
        print(f"  ⚠ {HONESTY_NOTE}\n")


if __name__ == "__main__":
    main()
