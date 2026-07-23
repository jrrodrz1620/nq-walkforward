# /// script
# requires-python = ">=3.10"
# dependencies = ["arch>=6.0", "pandas>=2.0", "numpy>=1.24", "matplotlib>=3.7"]
# ///
"""
garch_forecast.py — walk-forward GARCH(1,1) volatility forecasting.

What this does:
  Fits a GARCH(1,1) model walk-forward (no lookahead) and produces a
  1-day-ahead volatility forecast for every day in the sample.

What this does NOT do:
  Predict direction. GARCH forecasts the MAGNITUDE of moves, not which
  way they go. Every output of this module carries that note on purpose.

Usage:
  uv run garch_forecast.py --csv prices.csv            # date + close columns
  uv run garch_forecast.py --ticker BTC-USD            # via yfinance (needs internet)
  uv run garch_forecast.py --csv prices.csv --json     # machine-readable output
"""

import argparse
import json
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

TRADING_DAYS_CRYPTO = 365
TRADING_DAYS_EQUITY = 252
MIN_TRAIN = 500          # days of history before first forecast
REFIT_EVERY = 21         # re-estimate params every N days (walk-forward)
REGIME_LOOKBACK = 365    # window for vol percentile / regime classification

HONESTY_NOTE = "GARCH forecasts magnitude (volatility), not direction. It tells you how violent tomorrow is likely to be — not which way it goes."


def load_prices(csv=None, ticker=None):
    """Load a price series from CSV (date + close) or yfinance."""
    if csv:
        df = pd.read_csv(csv)
        cols = {c.lower().strip(): c for c in df.columns}
        date_col = next((cols[k] for k in ("date", "time", "timestamp") if k in cols), df.columns[0])
        px_col = next((cols[k] for k in ("close", "price", "priceusd", "adj close", "adj_close") if k in cols), df.columns[1])
        out = df[[date_col, px_col]].copy()
        out.columns = ["date", "close"]
    elif ticker:
        try:
            import yfinance as yf
        except ImportError:
            sys.exit("yfinance not installed. Use --csv, or: uv pip install yfinance")
        data = yf.download(ticker, period="max", auto_adjust=True, progress=False)
        out = data.reset_index()[["Date", "Close"]]
        out.columns = ["date", "close"]
    else:
        sys.exit("Provide --csv or --ticker")

    out["date"] = pd.to_datetime(out["date"])
    out = out.dropna().sort_values("date").reset_index(drop=True)
    out = out[out["close"] > 0]
    return out


def walkforward_garch(prices: pd.DataFrame, periods_per_year: int = TRADING_DAYS_CRYPTO,
                      min_train: int = MIN_TRAIN, refit_every: int = REFIT_EVERY) -> pd.DataFrame:
    """
    Walk-forward GARCH(1,1). For each day t >= min_train, forecast the vol of
    day t+1 using ONLY data available at the close of day t.

    Params are re-estimated every `refit_every` days on an expanding window.
    Between refits, the GARCH recursion is rolled forward with the last
    fitted params — still zero lookahead, because params were estimated on
    strictly prior data.

    Returns a DataFrame indexed like `prices` with:
      ret          — daily % return
      fcast_vol    — 1-day-ahead conditional vol forecast (daily, %)
      fcast_vol_ann— annualized forecast vol (%)
      vol_pctile   — percentile of today's forecast vs trailing REGIME_LOOKBACK
      regime       — calm / normal / storm
    """
    from arch import arch_model

    px = prices["close"].to_numpy(dtype=float)
    rets = 100.0 * np.diff(px) / px[:-1]           # daily % returns, scaled for arch
    n = len(rets)
    if n < min_train + 10:
        sys.exit(f"Need at least {min_train + 10} days of prices; got {n + 1}.")

    fcast_var = np.full(n, np.nan)                  # forecast of NEXT day's variance, made at t
    omega = alpha = beta = mu = None
    sigma2 = None

    for t in range(min_train, n):
        if (t - min_train) % refit_every == 0:
            am = arch_model(rets[:t], vol="GARCH", p=1, q=1, mean="Constant", dist="t")
            res = am.fit(disp="off", show_warning=False)
            p = res.params
            mu, omega, alpha, beta = p["mu"], p["omega"], p["alpha[1]"], p["beta[1]"]
            sigma2 = float(res.conditional_volatility[-1] ** 2)
        # roll the recursion one step with today's observed residual
        eps = rets[t] - mu if t > 0 else 0.0
        # sigma2 currently holds Var estimate for day t; update to t+1:
        sigma2 = omega + alpha * eps ** 2 + beta * sigma2
        fcast_var[t] = sigma2                        # made at close of t, for day t+1

    out = prices.iloc[1:].copy().reset_index(drop=True)
    out["ret"] = rets
    out["fcast_vol"] = np.sqrt(fcast_var)                                # daily %
    out["fcast_vol_ann"] = out["fcast_vol"] * np.sqrt(periods_per_year)  # annualized %
    pct = out["fcast_vol"].rolling(REGIME_LOOKBACK, min_periods=90).apply(
        lambda w: (w.iloc[:-1] < w.iloc[-1]).mean() * 100 if len(w) > 1 else np.nan, raw=False)
    out["vol_pctile"] = pct
    out["regime"] = pd.cut(out["vol_pctile"], bins=[-1, 33, 67, 101],
                           labels=["calm", "normal", "storm"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--ticker")
    ap.add_argument("--periods-per-year", type=int, default=TRADING_DAYS_CRYPTO,
                    help="365 for crypto (default), 252 for stocks")
    ap.add_argument("--json", action="store_true", help="print latest forecast as JSON")
    ap.add_argument("--out-csv", help="write full walk-forward series to CSV")
    args = ap.parse_args()

    prices = load_prices(csv=args.csv, ticker=args.ticker)
    res = walkforward_garch(prices, periods_per_year=args.periods_per_year)
    latest = res.dropna(subset=["fcast_vol"]).iloc[-1]

    payload = {
        "asset": args.ticker or args.csv,
        "as_of": str(latest["date"].date()),
        "forecast_vol_daily_pct": round(float(latest["fcast_vol"]), 3),
        "forecast_vol_annualized_pct": round(float(latest["fcast_vol_ann"]), 1),
        "vol_percentile_1y": round(float(latest["vol_pctile"]), 1) if pd.notna(latest["vol_pctile"]) else None,
        "regime": str(latest["regime"]),
        "note": HONESTY_NOTE,
    }
    if args.out_csv:
        res.to_csv(args.out_csv, index=False)
        payload["series_csv"] = args.out_csv
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\n  {payload['asset']} — as of {payload['as_of']}")
        print(f"  1-day vol forecast : {payload['forecast_vol_daily_pct']}% daily "
              f"({payload['forecast_vol_annualized_pct']}% annualized)")
        print(f"  vol percentile (1y): {payload['vol_percentile_1y']}")
        print(f"  regime             : {payload['regime'].upper()}")
        print(f"\n  ⚠ {HONESTY_NOTE}\n")


if __name__ == "__main__":
    main()
