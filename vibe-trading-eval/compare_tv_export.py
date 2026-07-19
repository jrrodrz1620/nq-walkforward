"""Head-to-head: a TradingView 'List of Trades' export vs this harness's fills.

    python vibe-trading-eval/compare_tv_export.py <export.xlsx> <ohlc.csv> [multiplier]

Loads the TradingView export with the same loader the app uses, reruns the
Python port over the supplied OHLC restricted to the export's date span, and
prints both sides' metrics plus a monthly-P&L correlation and an entry-time
match rate. A large gap in PF/net with a high entry-match rate means the two
disagree on FILLS (stops/targets/partials), not on signals — the divergence
that matters before trading live.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import Params, load_ohlc_csv, run_backtest  # noqa: E402
from dataio import load_and_clean  # noqa: E402
from metrics import calc_metrics  # noqa: E402

CAPITAL = 50_000.0


def summarize(trades: pd.DataFrame, label: str) -> dict:
    m = calc_metrics(trades, CAPITAL)
    print(f"── {label} ──")
    print(f"  trades {m['n_trades']}   net ${m['net_profit']:,.0f}   "
          f"WR {m['win_rate']:.1f}%   PF {m['profit_factor']:.2f}   "
          f"maxDD {m['max_dd']:.1f}%   expectancy ${m['expectancy']:,.0f}")
    return m


def monthly(trades: pd.DataFrame) -> pd.Series:
    return trades.groupby(trades["entry_time"].dt.to_period("M"))["profit_usd"].sum()


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    export_path, ohlc_path = sys.argv[1], sys.argv[2]
    multiplier = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0

    tv = load_and_clean(export_path, multiplier)
    if tv.empty:
        print("Could not parse the TradingView export (need Date/Time + Profit).")
        sys.exit(1)
    t0, t1 = tv["entry_time"].min(), tv["entry_time"].max()
    print(f"TradingView export: {len(tv)} trades  {t0.date()} -> {t1.date()}\n")

    ohlc = load_ohlc_csv(ohlc_path)
    ohlc = ohlc[(ohlc["time"] >= t0 - pd.Timedelta(days=30)) &
                (ohlc["time"] <= t1 + pd.Timedelta(days=1))].reset_index(drop=True)
    py = run_backtest(ohlc, Params(multiplier=multiplier))
    py = py[(py["entry_time"] >= t0) & (py["entry_time"] <= t1)].reset_index(drop=True)

    m_tv = summarize(tv, "TradingView fills")
    m_py = summarize(py, "Python harness fills (same span)")

    a, b = monthly(tv), monthly(py)
    both = pd.concat([a, b], axis=1, keys=["tv", "py"]).fillna(0.0)
    corr = both["tv"].corr(both["py"]) if len(both) > 2 else np.nan
    print(f"\nMonthly P&L correlation: {corr:.2f}   ({len(both)} months)")

    # Entry-time proximity: fraction of TV entries with a harness entry within 1 day
    py_times = py["entry_time"].sort_values().to_numpy()
    if len(py_times):
        idx = np.searchsorted(py_times, tv["entry_time"].to_numpy())
        idx = np.clip(idx, 0, len(py_times) - 1)
        prev = py_times[np.clip(idx - 1, 0, len(py_times) - 1)]
        near = np.minimum(np.abs(tv["entry_time"].to_numpy() - py_times[idx]),
                          np.abs(tv["entry_time"].to_numpy() - prev))
        match = float((near <= np.timedelta64(1, "D")).mean())
        print(f"TV entries with a harness entry within 1 day: {match:.0%}")

    print("\nReading: high entry match + big PF gap  -> fill-model divergence "
          "(TradingView optimistic fills).\n         low entry match           "
          "-> signal/data divergence (different bars or logic drift).")
    gap = m_tv["profit_factor"] - m_py["profit_factor"]
    print(f"PF gap (TV - harness): {gap:+.2f}")


if __name__ == "__main__":
    main()
