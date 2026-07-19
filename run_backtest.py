"""
Run the Phantom Flow SMC backtest and analyze it walk-forward — end to end.

    python run_backtest.py                  # synthetic OHLC
    python run_backtest.py --csv bars.csv   # your own OHLC CSV
    python run_backtest.py --xlsx out.xlsx  # also save a TradingView-style export

Produces REAL trades from the strategy logic (not synthetic outcomes), then
runs the same fold / metric / Monte Carlo stack the Streamlit app uses.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest import Params, generate_ohlc, load_ohlc_csv, run_backtest
from metrics import (split_folds, calc_metrics, monte_carlo,
                     permutation_test, bootstrap_sharpe_ci)

CAPITAL = 50000.0


def _pf(x: float) -> str:
    return "inf" if x == np.inf else f"{x:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="OHLC CSV (time,open,high,low,close)")
    ap.add_argument("--bars", type=int, default=4000, help="synthetic bars if no CSV")
    ap.add_argument("--xlsx", help="also write a TradingView-style trade export")
    args = ap.parse_args()

    if args.csv:
        ohlc = load_ohlc_csv(args.csv)
        src = args.csv
    else:
        ohlc = generate_ohlc(n_bars=args.bars)
        src = f"synthetic ({args.bars} bars)"

    p = Params()
    trades = run_backtest(ohlc, p)
    print(f"Source: {src}  →  {len(trades)} trades\n")

    if len(trades) < 10:
        print("Too few trades to analyze — adjust parameters or use more data.")
        return

    overall = calc_metrics(trades, CAPITAL)
    print("── BACKTEST (all trades) ─────────────────")
    print(f"  Trades        {overall['n_trades']}")
    print(f"  Net profit    ${overall['net_profit']:,.0f}")
    print(f"  Win rate      {overall['win_rate']:.1f}%")
    print(f"  Profit factor {_pf(overall['profit_factor'])}")
    print(f"  Expectancy    ${overall['expectancy']:,.0f}/trade  ({overall['avg_r']:.2f}R)")
    print(f"  Sharpe (trade){overall['sharpe']:.2f}")
    print(f"  Max DD        {overall['max_dd']:.1f}%")
    print(f"  Max loss strk {overall['max_loss_streak']}\n")

    folds = split_folds(trades, 5, 70, 5)
    if folds:
        oos = calc_metrics(pd.concat([f["test"] for f in folds]), CAPITAL)
        tr = calc_metrics(pd.concat([f["train"] for f in folds]), CAPITAL)
        ratio = oos["profit_factor"] / tr["profit_factor"] if tr["profit_factor"] > 0 else 0
        verdict = ("ROBUST" if ratio >= 0.8 else
                   "SOME DEGRADATION" if ratio >= 0.5 else "LIKELY OVERFIT")
        print("── WALK-FORWARD ──────────────────────────")
        print(f"  Folds         {len(folds)}")
        print(f"  OOS trades    {oos['n_trades']}")
        print(f"  OOS PF        {_pf(oos['profit_factor'])}")
        print(f"  OOS net       ${oos['net_profit']:,.0f}")
        print(f"  Train→OOS PF  {_pf(tr['profit_factor'])} → {_pf(oos['profit_factor'])}"
              f"  (ratio {ratio:.2f})  => {verdict}\n")

    mc = monte_carlo(trades["profit_usd"].to_numpy(), CAPITAL, n_sims=2000)
    print("── MONTE CARLO (2000 bootstraps) ─────────")
    print(f"  Final equity  p5 ${mc['final_p5']:,.0f} | p50 ${mc['final_p50']:,.0f} | p95 ${mc['final_p95']:,.0f}")
    print(f"  Max drawdown  p95 {mc['maxdd_p95']:.1f}%   P(loss) {mc['prob_loss']:.1f}%\n")

    pnl = trades["profit_usd"].to_numpy()
    perm = permutation_test(pnl, CAPITAL, n_sims=2000)
    ci = bootstrap_sharpe_ci(pnl, n_boot=2000)
    if perm and ci:
        print("── SIGNIFICANCE ──────────────────────────")
        print(f"  Permutation   path Sharpe {perm['actual_sharpe']:.3f}"
              f"  p={perm['p_value_sharpe']:.2f}   maxDD {perm['actual_maxdd']:.1f}%"
              f"  p={perm['p_value_maxdd']:.2f}")
        print(f"  Sharpe {ci['confidence']:.0%} CI [{ci['ci_lower']:.2f}, {ci['ci_upper']:.2f}]"
              f"   P(Sharpe>0) {ci['prob_positive']:.1%}")

    if args.xlsx:
        export = trades.rename(columns={
            "trade_num": "Trade #", "type": "Type", "signal": "Signal",
            "entry_time": "Date/Time", "entry_price": "Price", "profit_usd": "Profit",
        })[["Trade #", "Type", "Signal", "Date/Time", "Price", "Profit"]]
        export["Cum. Profit"] = export["Profit"].cumsum().round(2)
        export.to_excel(args.xlsx, index=False, sheet_name="List of Trades")
        print(f"\nWrote TradingView-style export → {args.xlsx}")


if __name__ == "__main__":
    main()
