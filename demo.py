"""
CLI demo of the full walk-forward pipeline — no TradingView, no browser.

    python demo.py

Generates a sample trade export, runs it through the exact loader and metric
helpers the Streamlit app uses, and prints the per-fold table, OOS summary,
overfit verdict and a Monte Carlo band.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dataio import load_and_clean
from metrics import split_folds, calc_metrics, monte_carlo
from sample_data import save_sample_xlsx

CAPITAL = 50000.0
MULT = 20.0          # NQ
N_FOLDS = 5
TRAIN_PCT = 70
MIN_TRADES = 5


def _pf(x: float) -> str:
    return "inf" if x == np.inf else f"{x:.2f}"


def main() -> None:
    path = save_sample_xlsx("sample_trades.xlsx", n=220, seed=7,
                            contract_multiplier=MULT)
    df = load_and_clean(path, MULT)
    print(f"Loaded {len(df)} trades from {path}\n")

    overall = calc_metrics(df, CAPITAL)
    print("── OVERALL ───────────────────────────────")
    print(f"  Trades        {overall['n_trades']}")
    print(f"  Net profit    ${overall['net_profit']:,.0f}")
    print(f"  Win rate      {overall['win_rate']:.1f}%")
    print(f"  Profit factor {_pf(overall['profit_factor'])}")
    print(f"  Expectancy    ${overall['expectancy']:,.0f}/trade  ({overall['avg_r']:.2f}R)")
    print(f"  Payoff ratio  {_pf(overall['payoff'])}")
    print(f"  Sharpe (trade){overall['sharpe']:.2f}")
    print(f"  Max DD        {overall['max_dd']:.1f}%")
    print(f"  Max loss strk {overall['max_loss_streak']}\n")

    folds = split_folds(df, N_FOLDS, TRAIN_PCT, MIN_TRADES)
    print(f"── PER-FOLD ({len(folds)} folds, {TRAIN_PCT}% train) ──────────")
    hdr = f"  {'Fold':>4} {'TrTr':>5} {'TrPF':>6} {'OOSn':>5} {'OOSwr':>6} {'OOSpf':>6} {'OOS$':>9}"
    print(hdr)
    for f in folds:
        tm = calc_metrics(f["train"], CAPITAL)
        om = calc_metrics(f["test"], CAPITAL)
        print(f"  {f['fold']:>4} {tm['n_trades']:>5} {_pf(tm['profit_factor']):>6} "
              f"{om['n_trades']:>5} {om['win_rate']:>5.1f}% {_pf(om['profit_factor']):>6} "
              f"{om['net_profit']:>9,.0f}")

    oos_all = pd.concat([f["test"] for f in folds])
    train_all = pd.concat([f["train"] for f in folds])
    oos = calc_metrics(oos_all, CAPITAL)
    tr = calc_metrics(train_all, CAPITAL)

    print("\n── OUT-OF-SAMPLE SUMMARY ─────────────────")
    print(f"  OOS trades    {oos['n_trades']}")
    print(f"  OOS net       ${oos['net_profit']:,.0f}")
    print(f"  OOS win rate  {oos['win_rate']:.1f}%")
    print(f"  OOS PF        {_pf(oos['profit_factor'])}")
    print(f"  OOS Sharpe    {oos['sharpe']:.2f}")
    print(f"  OOS max DD    {oos['max_dd']:.1f}%")

    ratio = oos["profit_factor"] / tr["profit_factor"] if tr["profit_factor"] > 0 else 0
    verdict = ("ROBUST" if ratio >= 0.8 else
               "SOME DEGRADATION" if ratio >= 0.5 else "LIKELY OVERFIT")
    print(f"\n  Train PF {_pf(tr['profit_factor'])} -> OOS PF {_pf(oos['profit_factor'])}"
          f"  (ratio {ratio:.2f})  => {verdict}")

    mc = monte_carlo(df["profit_usd"].to_numpy(), CAPITAL, n_sims=2000)
    print("\n── MONTE CARLO (2000 bootstraps) ─────────")
    print(f"  Final equity  p5 ${mc['final_p5']:,.0f} | p50 ${mc['final_p50']:,.0f} | p95 ${mc['final_p95']:,.0f}")
    print(f"  Max drawdown  p5 {mc['maxdd_p5']:.1f}% | p50 {mc['maxdd_p50']:.1f}% | p95 {mc['maxdd_p95']:.1f}%")
    print(f"  P(end below start capital)  {mc['prob_loss']:.1f}%")


if __name__ == "__main__":
    main()
