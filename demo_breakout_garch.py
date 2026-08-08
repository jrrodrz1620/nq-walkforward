#!/usr/bin/env python3
"""
demo_breakout_garch.py — a self-contained trend/breakout example for
garch_size_review.py, showing the case where GARCH vol-targeting *helps*.

Why this exists:
  garch_size_review.py answers "does vol-targeting improve this strategy?" On a
  mean-reversion strategy that thrives in high volatility (like the SQHMM export)
  the honest answer is "no". This demo builds the opposite kind of strategy — a
  trend-follower that loses in high-vol whipsaw and makes its money riding calm
  trends — so you can see the verdict flip to "yes".

What it generates (stdlib only, no heavy deps):
  * a synthetic daily price series with regime-switching volatility:
      - trend regime : low vol, strong persistent drift  -> breakouts follow through
      - storm regime : high vol, mean-reverting whipsaw   -> breakouts fail
  * a Donchian 20/10 breakout strategy (long & short) traded on that series,
    written as a TradingView-style "List of Trades" export.

Both files land next to each other so garch_size_review.py can consume them:

  python demo_breakout_garch.py --out /tmp/demo
  python garch_size_review.py /tmp/demo/breakout_trades.csv \
         --prices /tmp/demo/breakout_prices.csv --targets 12,18,25

Expected verdict: vol-targeting improves per-trade Sharpe and roughly halves max
drawdown, because the strategy's losses are concentrated in the 'storm' regime.
(Because low-frequency breakout strategies produce few trades, pass --prices so
GARCH fits the real daily bars rather than the sparse trade-price proxy.)
"""
from __future__ import annotations

import argparse
import csv
import datetime
import math
import os
import random

POINT_VALUE = 20.0     # $ per index point per contract (E-mini NQ-like)
DONCHIAN_ENTRY = 20    # breakout lookback
DONCHIAN_EXIT = 10     # opposite-extreme exit lookback


def synth_prices(n: int, seed: int, start: datetime.date):
    """Regime-switching daily closes: calm trends punctuated by high-vol storms."""
    rng = random.Random(seed)
    px = 15000.0
    dates, closes = [], []
    storm = False
    drift_sign = 1
    last_ret = 0.0
    for i in range(n):
        if storm:
            if rng.random() < 0.10:      # storms last ~10 days
                storm = False
            vol, mu = 0.030, -0.7 * last_ret          # whipsaw / mean-revert
        else:
            if rng.random() < 0.012:     # storms are infrequent
                storm = True
            if rng.random() < 0.008:     # trends last ~125 days
                drift_sign *= -1
            vol, mu = 0.008, 0.0011 * drift_sign      # strong persistent drift
        r = mu + rng.gauss(0, vol)
        px *= math.exp(r)
        last_ret = r
        dates.append(start + datetime.timedelta(days=i))
        closes.append(px)
    return dates, closes


def donchian_breakout(dates, closes):
    """Donchian 20/10 breakout, long & short, always-in after first signal."""
    trades = []
    pos, entry_px = 0, None
    for i in range(DONCHIAN_ENTRY, len(closes)):
        hi = max(closes[i - DONCHIAN_ENTRY:i])
        lo = min(closes[i - DONCHIAN_ENTRY:i])
        ex_hi = max(closes[i - DONCHIAN_EXIT:i])
        ex_lo = min(closes[i - DONCHIAN_EXIT:i])
        c = closes[i]
        if pos == 0:
            if c > hi:
                pos, entry_px = 1, c
            elif c < lo:
                pos, entry_px = -1, c
        elif pos == 1 and c < ex_lo:
            trades.append((dates[i], c, (c - entry_px) * POINT_VALUE, "long"))
            pos = 0
            if c < lo:
                pos, entry_px = -1, c
        elif pos == -1 and c > ex_hi:
            trades.append((dates[i], c, (entry_px - c) * POINT_VALUE, "short"))
            pos = 0
            if c > hi:
                pos, entry_px = 1, c
    return trades


def write_prices(path: str, dates, closes):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "close"])
        for d, c in zip(dates, closes):
            w.writerow([d, round(c, 2)])


def write_trades(path: str, trades):
    cum = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Trade number", "Type", "Date and time", "Signal",
                    "Price USD", "Size (qty)", "Net PnL USD", "Cumulative PnL USD"])
        for k, (d, xp, pnl, side) in enumerate(trades, 1):
            cum += pnl
            w.writerow([k, f"Entry {side}", f"{d} 15:00", "Breakout",
                        round(xp, 2), 1, round(pnl, 2), round(cum, 2)])
    return cum


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=".", help="output directory (default: cwd)")
    ap.add_argument("--days", type=int, default=2600, help="daily bars to generate")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--start", default="2017-01-01", help="first date (YYYY-MM-DD)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    start = datetime.date.fromisoformat(args.start)
    dates, closes = synth_prices(args.days, args.seed, start)
    trades = donchian_breakout(dates, closes)

    prices_path = os.path.join(args.out, "breakout_prices.csv")
    trades_path = os.path.join(args.out, "breakout_trades.csv")
    write_prices(prices_path, dates, closes)
    net = write_trades(trades_path, trades)

    wins = sum(1 for t in trades if t[2] > 0)
    print(f"prices : {prices_path}  ({len(dates)} daily bars, "
          f"{dates[0]}..{dates[-1]})")
    print(f"trades : {trades_path}  ({len(trades)} breakout trades, "
          f"net ${net:,.0f}, win {100*wins/len(trades):.0f}%)")
    print("\nNow run the sizing review on it:")
    print(f"  python garch_size_review.py {trades_path} \\")
    print(f"         --prices {prices_path} --targets 12,18,25")


if __name__ == "__main__":
    main()
