# /// script
# requires-python = ">=3.10"
# dependencies = ["arch>=6.0", "pandas>=2.0", "numpy>=1.24"]
# ///
"""
vol_target.py — turn a volatility forecast into a position size.

The entire idea in one line:
    size = target_vol / forecast_vol      (capped so it never does anything insane)

Storm coming -> smaller position. Calm ahead -> bigger position.
Same trades, different sizes. This is the "how much" answer.

Usage:
  uv run vol_target.py --csv prices.csv --target-vol 15
  uv run vol_target.py --csv prices.csv --target-vol 15 --json
"""

import argparse
import json

import numpy as np
import pandas as pd

MAX_LEVERAGE = 2.0
MIN_SIZE = 0.25


def size_from_vol(forecast_vol_ann: float, target_vol_ann: float = 15.0,
                  max_leverage: float = MAX_LEVERAGE, min_size: float = MIN_SIZE) -> float:
    """Position size multiplier from an annualized vol forecast (%)."""
    if forecast_vol_ann is None or forecast_vol_ann <= 0 or np.isnan(forecast_vol_ann):
        return min_size
    return float(np.clip(target_vol_ann / forecast_vol_ann, min_size, max_leverage))


def size_series(fcast_vol_ann: pd.Series, target_vol_ann: float = 15.0,
                max_leverage: float = MAX_LEVERAGE, min_size: float = MIN_SIZE) -> pd.Series:
    """Vectorized version for backtests."""
    s = target_vol_ann / fcast_vol_ann
    return s.clip(lower=min_size, upper=max_leverage).fillna(min_size)


def main():
    from garch_forecast import load_prices, walkforward_garch, HONESTY_NOTE

    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--ticker")
    ap.add_argument("--target-vol", type=float, default=15.0,
                    help="annualized target vol %% (default 15)")
    ap.add_argument("--periods-per-year", type=int, default=365)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    prices = load_prices(csv=args.csv, ticker=args.ticker)
    res = walkforward_garch(prices, periods_per_year=args.periods_per_year)
    latest = res.dropna(subset=["fcast_vol"]).iloc[-1]
    mult = size_from_vol(float(latest["fcast_vol_ann"]), args.target_vol)

    payload = {
        "as_of": str(latest["date"].date()),
        "forecast_vol_annualized_pct": round(float(latest["fcast_vol_ann"]), 1),
        "target_vol_pct": args.target_vol,
        "position_size_multiplier": round(mult, 2),
        "read_as": f"run {round(mult, 2)}x your baseline position size",
        "caps": {"max": MAX_LEVERAGE, "min": MIN_SIZE},
        "note": HONESTY_NOTE,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\n  as of {payload['as_of']}: forecast vol {payload['forecast_vol_annualized_pct']}% "
              f"vs target {args.target_vol}%")
        print(f"  → position size: {payload['position_size_multiplier']}x baseline\n")


if __name__ == "__main__":
    main()
