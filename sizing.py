"""
Risk-based position sizing for NQ / MNQ (and any futures contract).

The backtest treats `contracts` as a fixed input — it never *sizes* anything.
This module answers the real question: given your account, how much risk you'll
take per trade, and how far away the stop sits, how many contracts should you
trade?

    contracts = floor( (equity × risk_pct) / (stop_pts × point_value) )

Point values: NQ = $20/pt, MNQ = $2/pt, ES = $50/pt, MES = $5/pt.

The stop distance is the one strategy-specific input. Rather than guess it,
`stop_pts_from_backtest` reads the median initial stop distance straight out of
a `run_backtest` trade list, so the size is anchored to how the strategy
actually behaves on your bars.

CLI:
    python sizing.py --account 100000 --risk 1.0            # synthetic bars
    python sizing.py --account 100000 --risk 1.0 --csv nq.csv
    python sizing.py --account 100000 --risk 1.0 --stop-pts 40
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Dollar value of a one-point move, per contract.
POINT_VALUE = {"NQ": 20.0, "MNQ": 2.0, "ES": 50.0, "MES": 5.0}


@dataclass
class Sizing:
    symbol: str
    contracts: int          # whole contracts to trade (floored)
    point_value: float
    stop_pts: float
    risk_per_contract: float  # $ at risk if one contract is stopped out
    dollar_risk: float        # $ at risk for the sized position
    risk_pct_actual: float    # dollar_risk as % of equity (after flooring)
    account: float
    risk_pct_target: float


def size_position(account: float, risk_pct: float, stop_pts: float,
                  symbol: str = "NQ", point_value: float | None = None) -> Sizing:
    """Whole-contract size that risks about `risk_pct`% of `account` at the stop.

    `risk_pct` is a percent (1.0 == 1%). Contracts floor to a whole number, so
    the realized risk is <= the target; a stop so wide that even one contract
    exceeds the budget floors to 0 (i.e. "don't take it on this symbol").
    """
    if account <= 0:
        raise ValueError("account must be positive")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive")
    if stop_pts <= 0:
        raise ValueError("stop_pts must be positive")
    pv = point_value if point_value is not None else POINT_VALUE.get(symbol.upper())
    if pv is None:
        raise KeyError(f"unknown symbol {symbol!r}; pass point_value= explicitly")

    budget = account * (risk_pct / 100.0)
    risk_per_contract = stop_pts * pv
    contracts = int(math.floor(budget / risk_per_contract))
    dollar_risk = contracts * risk_per_contract
    return Sizing(
        symbol=symbol.upper(),
        contracts=contracts,
        point_value=pv,
        stop_pts=round(stop_pts, 2),
        risk_per_contract=round(risk_per_contract, 2),
        dollar_risk=round(dollar_risk, 2),
        risk_pct_actual=round(100.0 * dollar_risk / account, 3),
        account=account,
        risk_pct_target=risk_pct,
    )


def stop_pts_from_backtest(trades: pd.DataFrame, stat: str = "median") -> float:
    """Representative stop distance (points) from a `run_backtest` trade list.

    Uses the `stop_pts` column recorded per trade. `stat` is "median" (robust,
    default) or "mean".
    """
    if "stop_pts" not in trades.columns:
        raise KeyError("trades has no 'stop_pts' column — run backtest.run_backtest")
    s = trades["stop_pts"].dropna().to_numpy()
    if len(s) == 0:
        raise ValueError("no trades to derive a stop distance from")
    return float(np.median(s) if stat == "median" else np.mean(s))


def _report(account: float, risk_pct: float, stop_pts: float,
            stop_source: str) -> None:
    nq = size_position(account, risk_pct, stop_pts, "NQ")
    mnq = size_position(account, risk_pct, stop_pts, "MNQ")
    budget = account * (risk_pct / 100.0)

    print("── NQ POSITION SIZING ────────────────────")
    print(f"  Account        ${account:,.0f}")
    print(f"  Risk / trade   {risk_pct:.2f}%  (${budget:,.0f})")
    print(f"  Stop distance  {stop_pts:.1f} pts   [{stop_source}]")
    print()
    for s in (nq, mnq):
        line = (f"  {s.symbol:<4}  {s.contracts:>3} contract(s)"
                f"   risk/contract ${s.risk_per_contract:,.0f}"
                f"   position risk ${s.dollar_risk:,.0f}"
                f"  ({s.risk_pct_actual:.2f}%)")
        if s.contracts == 0:
            line += "   → stop too wide for one contract at this risk"
        print(line)
    if nq.contracts == 0 and mnq.contracts > 0:
        print("\n  → Full NQ risks more than your per-trade budget. Trade MNQ.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Risk-based NQ/MNQ position sizing.")
    ap.add_argument("--account", type=float, required=True, help="account equity in $")
    ap.add_argument("--risk", type=float, default=1.0, help="risk per trade in %% (default 1)")
    ap.add_argument("--stop-pts", type=float, help="stop distance in points (skip backtest)")
    ap.add_argument("--csv", help="OHLC CSV to derive stop distance from the strategy")
    ap.add_argument("--bars", type=int, default=4000, help="synthetic bars if no CSV/stop-pts")
    ap.add_argument("--stat", choices=("median", "mean"), default="median")
    args = ap.parse_args()

    if args.stop_pts is not None:
        stop_pts, source = args.stop_pts, "user-supplied"
    else:
        # Derive the stop distance from the strategy's own behavior.
        from backtest import Params, generate_ohlc, load_ohlc_csv, run_backtest
        if args.csv:
            ohlc = load_ohlc_csv(args.csv)
            label = args.csv
        else:
            ohlc = generate_ohlc(n_bars=args.bars)
            label = f"synthetic {args.bars} bars — supply --csv for real NQ data"
        trades = run_backtest(ohlc, Params())
        if len(trades) == 0:
            raise SystemExit("Backtest produced no trades; pass --stop-pts instead.")
        stop_pts = stop_pts_from_backtest(trades, args.stat)
        source = f"{args.stat} of {len(trades)} backtest trades — {label}"

    _report(args.account, args.risk, stop_pts, source)


if __name__ == "__main__":
    main()
