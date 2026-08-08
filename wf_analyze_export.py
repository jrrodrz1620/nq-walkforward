#!/usr/bin/env python3
"""
wf_analyze_export.py — full walk-forward audit of a TradingView "List of Trades"
export, in one command.

Runs, on real trades:
  1. Overall performance metrics (net, PF, payoff, expectancy, Sharpe, streaks, DD)
  2. Walk-forward folds — train PF vs out-of-sample test PF + the optimism gap
  3. Monte Carlo bootstrap — outcome range + probability of ending below capital
  4. Profit concentration — how much of net P&L rides on the top few trades
  5. Slippage sensitivity — how much per-leg slippage the edge can absorb
  6. Robustness cuts — per-fold drawdown, and P&L by hour-of-day

All the heavy lifting reuses the repo's own modules (dataio, metrics) so the
numbers match app.py / demo.py exactly.

Usage:
  python wf_analyze_export.py trades.csv
  python wf_analyze_export.py trades.csv --capital 100000 --folds 5 --chart eq.png
  python wf_analyze_export.py trades.csv --point-value 1   # $/point/contract for slippage
  python wf_analyze_export.py trades.csv --json report.json

Notes:
  * --capital: starting equity for return %/drawdown. If the export's final
    "Cumulative PnL %" implies a round number, that's usually the right value
    (e.g. cum PnL 101,131 at 101.13% -> 100,000).
  * --point-value: dollars per 1.0 price point per 1 contract, used ONLY for the
    slippage sensitivity ($/point). CFD/spread-bet NASDAQ is ~1; E-mini NQ is 20.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from dataio import load_and_clean
from metrics import calc_metrics, split_folds, monte_carlo


def _fmt(x):
    return f"{x:,.2f}" if isinstance(x, (int, float, np.floating)) else str(x)


def per_trade_qty(csv_path: str, df: pd.DataFrame) -> pd.Series:
    """Recover per-trade contract quantity from the raw export, aligned to df."""
    try:
        raw = pd.read_csv(csv_path)
    except Exception:
        return pd.Series(1.0, index=df.index)
    qty_col = next((c for c in raw.columns if c.strip() in
                    ("Size (qty)", "Contracts", "Qty", "Quantity")), None)
    num_col = next((c for c in raw.columns if c.strip() in
                    ("Trade number", "Trade #")), None)
    if qty_col is None or num_col is None or "trade_num" not in df.columns:
        return pd.Series(1.0, index=df.index)
    raw = raw[pd.to_numeric(raw[num_col], errors="coerce").notna()]
    q = raw.groupby(pd.to_numeric(raw[num_col]))[qty_col].first()
    return df["trade_num"].map(q).astype(float).fillna(1.0)


def slippage_sensitivity(pnl: np.ndarray, qty: np.ndarray, point_value: float,
                         legs: int = 2) -> dict:
    """Net P&L and expectancy after assumed per-leg slippage (points)."""
    rows = {}
    for s in (0.10, 0.25, 0.50, 1.00, 1.50, 2.00):
        cost = legs * s * qty * point_value
        adj = pnl - cost
        rows[s] = {"cost_per_trade_median": float(legs * s * np.median(qty) * point_value),
                   "net": float(adj.sum()), "expectancy": float(adj.mean()),
                   "positive": bool(adj.mean() > 0)}
    exp0 = pnl.mean()
    breakeven = exp0 / (legs * qty.mean() * point_value) if qty.mean() > 0 else float("nan")
    return {"rows": rows, "breakeven_pts_per_leg": float(breakeven)}


def hour_of_day(df: pd.DataFrame) -> pd.DataFrame:
    """P&L grouped by entry hour (whatever tz the export is in)."""
    d = df.copy()
    d["hour"] = d["entry_time"].dt.hour
    g = d.groupby("hour")["profit_usd"].agg(["count", "sum", "mean"])
    g.columns = ["n_trades", "net_usd", "expectancy_usd"]
    return g


def concentration(pnl: np.ndarray) -> dict:
    s = np.sort(pnl)[::-1]
    tot = s.sum()
    return {k: float(100 * s[:k].sum() / tot) for k in (1, 5, 10, 20) if k <= len(s)} \
        if tot != 0 else {}


def analyze(csv_path: str, capital: float, n_folds: int, train_pct: float,
            point_value: float, chart: str | None) -> dict:
    df = load_and_clean(csv_path, contract_multiplier=max(point_value, 1e-9))
    if df.empty:
        raise SystemExit("No trades parsed — is this a TradingView List of Trades export?")

    qty = per_trade_qty(csv_path, df).to_numpy()
    pnl = df["profit_usd"].to_numpy(dtype=float)
    overall = calc_metrics(df, capital)

    folds = split_folds(df, n=n_folds, train_pct=train_pct, min_trades=20)
    fold_rows, gaps, oos_net = [], [], 0.0
    for f in folds:
        tr, te = calc_metrics(f["train"], capital), calc_metrics(f["test"], capital)
        gaps.append(tr["profit_factor"] - te["profit_factor"])
        oos_net += te["net_profit"]
        fold_rows.append({
            "fold": f["fold"],
            "test_window": f"{f['test_start'].date()}->{f['test_end'].date()}",
            "train_pf": tr["profit_factor"], "test_pf": te["profit_factor"],
            "train_exp": tr["expectancy"], "test_exp": te["expectancy"],
            "test_net": te["net_profit"], "test_max_dd": te["max_dd"],
        })

    mc = monte_carlo(pnl, capital, n_sims=1000)
    slip = slippage_sensitivity(pnl, qty, point_value)
    hod = hour_of_day(df)

    report = {
        "file": csv_path, "capital": capital,
        "date_range": [str(df["entry_time"].min().date()), str(df["entry_time"].max().date())],
        "trading_days": int(df["entry_time"].dt.date.nunique()),
        "overall": {k: (float(v) if isinstance(v, (int, float, np.floating)) else None)
                    for k, v in overall.items() if k != "equity"},
        "folds": fold_rows,
        "mean_optimism_gap": float(np.mean(gaps)) if gaps else None,
        "oos_summed_net": float(oos_net),
        "monte_carlo": {k: v for k, v in mc.items() if k not in ("finals", "max_dds")},
        "concentration_pct": concentration(pnl),
        "slippage": slip,
        "point_value": point_value,
    }

    _print_report(report, hod)
    if chart:
        _plot(df, capital, chart)
        report["chart"] = chart
    return report


def _print_report(r: dict, hod: pd.DataFrame):
    o = r["overall"]
    print(f"\n{'='*62}\n  {r['file']}")
    print(f"  {r['date_range'][0]} -> {r['date_range'][1]}  "
          f"({int(o['n_trades'])} trades, {r['trading_days']} trading days, "
          f"${r['capital']:,.0f} capital)\n{'='*62}")

    print("\n-- OVERALL --")
    for k in ("net_profit", "return_pct", "win_rate", "profit_factor", "payoff",
              "expectancy", "sharpe", "avg_win", "avg_loss", "max_dd",
              "max_win_streak", "max_loss_streak"):
        print(f"  {k:16} {_fmt(o[k])}")

    print("\n-- WALK-FORWARD FOLDS (train PF -> out-of-sample test PF) --")
    print(f"  {'fold':>4} {'train_PF':>9} {'test_PF':>8} {'test_exp$':>10} "
          f"{'test_net$':>10} {'test_DD%':>8}  window")
    for f in r["folds"]:
        print(f"  {f['fold']:>4} {f['train_pf']:>9.2f} {f['test_pf']:>8.2f} "
              f"{f['test_exp']:>10,.0f} {f['test_net']:>10,.0f} {f['test_max_dd']:>8.2f}"
              f"  {f['test_window']}")
    print(f"\n  mean optimism gap (train PF - test PF): {r['mean_optimism_gap']:+.2f}"
          f"   (negative = OOS held up)")
    print(f"  summed out-of-sample net: ${r['oos_summed_net']:,.0f}")

    mc = r["monte_carlo"]
    print("\n-- MONTE CARLO (1000 bootstraps) --")
    print(f"  final equity  p5 ${mc['final_p5']:,.0f} | p50 ${mc['final_p50']:,.0f} "
          f"| p95 ${mc['final_p95']:,.0f}")
    print(f"  max drawdown  p5 {mc['maxdd_p5']:.1f}% | p50 {mc['maxdd_p50']:.1f}% "
          f"| p95 {mc['maxdd_p95']:.1f}%")
    print(f"  prob(end < start capital): {mc['prob_loss']:.1f}%")

    print("\n-- PROFIT CONCENTRATION --")
    for k, v in r["concentration_pct"].items():
        print(f"  top {k:>2} trades = {v:5.1f}% of net")

    s = r["slippage"]
    print(f"\n-- SLIPPAGE SENSITIVITY (${r['point_value']:g}/pt/contract, round-trip) --")
    print(f"  {'pts/leg':>8} {'$/trade':>9} {'net':>12} {'expectancy':>11} {'+edge?':>7}")
    for pts, row in s["rows"].items():
        print(f"  {pts:>8.2f} {row['cost_per_trade_median']:>9.0f} {row['net']:>12,.0f} "
              f"{row['expectancy']:>11.1f} {'yes' if row['positive'] else 'NO':>7}")
    print(f"  break-even slippage: {s['breakeven_pts_per_leg']:.2f} pts/leg")

    print("\n-- P&L BY HOUR OF DAY (export tz) --")
    print(f"  {'hour':>4} {'n':>5} {'net$':>10} {'exp$':>8}")
    for h, row in hod.iterrows():
        print(f"  {int(h):>4} {int(row['n_trades']):>5} {row['net_usd']:>10,.0f} "
              f"{row['expectancy_usd']:>8.1f}")


def _plot(df: pd.DataFrame, capital: float, out: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = df["entry_time"]
    eq = capital + df["profit_usd"].cumsum().to_numpy()
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100

    fig, (a, b) = plt.subplots(2, 1, figsize=(12, 7), height_ratios=[3, 1],
                               facecolor="white", sharex=True)
    a.plot(t, eq, lw=1.5, color="#0a7d38")
    a.set_ylabel(f"Equity (${capital:,.0f} start)")
    a.set_title("Walk-forward equity", loc="left", fontweight="bold")
    a.grid(alpha=.25, lw=.5)
    b.fill_between(t, dd, 0, color="#d62728", alpha=.35)
    b.set_ylabel("Drawdown %")
    b.grid(alpha=.25, lw=.5)
    for ax in (a, b):
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="TradingView List of Trades CSV/XLSX")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--train-pct", type=float, default=70.0)
    ap.add_argument("--point-value", type=float, default=1.0,
                    help="$/point/contract for slippage (CFD~1, E-mini NQ=20)")
    ap.add_argument("--chart", help="write equity+drawdown PNG here")
    ap.add_argument("--json", help="write full report JSON here")
    args = ap.parse_args()

    report = analyze(args.export, args.capital, args.folds, args.train_pct,
                     args.point_value, args.chart)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n  report JSON -> {args.json}")


if __name__ == "__main__":
    main()
