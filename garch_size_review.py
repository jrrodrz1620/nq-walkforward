#!/usr/bin/env python3
"""
garch_size_review.py — does GARCH vol-targeting improve THIS strategy?

Takes a TradingView "List of Trades" export and answers one question honestly:
would sizing the strategy by a walk-forward GARCH volatility forecast have made
it better, worse, or no different? It also shows how the strategy performs in
each volatility regime, which usually explains the answer.

How it works (no external price data needed — uses the export's own prices):
  1. Build a daily close proxy from the trade prices in the export.
  2. Fit walk-forward GARCH(1,1) on that series (zero lookahead) -> a 1-day-ahead
     annualized vol forecast + calm/normal/storm regime for each day.
  3. For each trade, look up the forecast as of the trade date, and scale the
     trade's realized P&L by size = target_vol / forecast_vol (capped 0.25-2.0x).
     P&L scales linearly with size, so scaled P&L is the vol-targeted outcome.
  4. Compare fixed vs vol-targeted (net, expectancy, per-trade Sharpe, max DD),
     and break the fixed strategy's P&L down by regime.

Interpretation:
  * If per-trade Sharpe is ~unchanged, vol-targeting only rescales the strategy
    and adds nothing — leave sizing alone.
  * If the strategy makes MORE in "storm" regimes, vol-targeting (which shrinks
    size when vol is high) will actively hurt it.
  * Vol-targeting helps strategies that get *hurt* by high volatility (trend /
    breakout), not ones that feed on it (mean-reversion / regime).

CAVEAT: trade-time prices are not clean daily settlement closes, so vol *levels*
and regime *labels* are approximate. The Sharpe-invariance and regime-tilt
signals are robust to that; don't read the exact vol numbers as gospel.

Usage:
  python garch_size_review.py trades.csv
  python garch_size_review.py trades.csv --targets 10,15,20 --regime-lookback 126
  python garch_size_review.py trades.csv --periods-per-year 252 --json review.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

# The GARCH tooling lives in scripts/ (vendored garch-method plugin).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import garch_forecast as gf            # noqa: E402
from vol_target import size_from_vol   # noqa: E402

from dataio import load_and_clean      # noqa: E402


def _stats(pnl: np.ndarray, capital: float) -> dict:
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) == 0:
        return {"net": 0.0, "exp": 0.0, "sharpe": 0.0, "maxdd": 0.0, "final": capital}
    eq = capital + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    maxdd = float(((eq - peak) / peak * 100).min())
    sd = pnl.std(ddof=1) if len(pnl) > 1 else 0.0
    sharpe = float(pnl.mean() / sd) if sd > 0 else 0.0
    return {"net": float(pnl.sum()), "exp": float(pnl.mean()),
            "sharpe": sharpe, "maxdd": maxdd, "final": float(eq[-1])}


def _load_daily_prices(prices_csv: str) -> pd.DataFrame:
    """Load a real daily price series (date + close columns, any casing)."""
    raw = pd.read_csv(prices_csv)
    cols = {c.lower().strip(): c for c in raw.columns}
    dcol = next((cols[k] for k in ("date", "time", "timestamp") if k in cols), raw.columns[0])
    ccol = next((cols[k] for k in ("close", "price", "adj close", "adj_close") if k in cols),
                raw.columns[1])
    out = raw[[dcol, ccol]].copy()
    out.columns = ["date", "close"]
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out = out.dropna().sort_values("date").reset_index(drop=True)
    return out[out["close"] > 0]


def review(csv_path: str, capital: float, targets: list[float], min_train: int,
           regime_lookback: int, periods_per_year: int, prices_csv: str | None = None) -> dict:
    df = load_and_clean(csv_path, contract_multiplier=1.0)
    if df.empty:
        raise SystemExit("No trades parsed — is this a TradingView List of Trades export?")
    df = df.copy()
    df["date"] = pd.to_datetime(df["entry_time"]).dt.normalize()

    # 1) price series for GARCH: prefer real daily bars, else proxy from trade prices.
    #    The proxy is fine for high-frequency strategies (many trades/day of coverage)
    #    but too sparse for low-frequency ones (breakout/swing) — pass --prices there.
    if prices_csv:
        daily = _load_daily_prices(prices_csv)
        price_source = f"real daily bars ({prices_csv})"
    else:
        if "entry_price" not in df.columns:
            raise SystemExit("Export has no price column — pass --prices with daily bars.")
        daily = (df.groupby(df["date"])["entry_price"].last()
                   .reset_index().sort_values("date").reset_index(drop=True))
        daily.columns = ["date", "close"]
        price_source = "trade-price proxy (approximate; pass --prices for real bars)"

    # 2) walk-forward GARCH (regime lookback shortened so it classifies short series)
    gf.REGIME_LOOKBACK = regime_lookback
    res = gf.walkforward_garch(daily, periods_per_year=periods_per_year,
                               min_train=min_train, refit_every=21)
    res = res.dropna(subset=["fcast_vol_ann"]).copy()
    res["date"] = pd.to_datetime(res["date"]).dt.normalize()
    if res.empty:
        raise SystemExit(f"Not enough price history for GARCH "
                         f"({len(daily)} days, need > {min_train}). Lower --min-train.")

    # 3) as-of forecast for each trade (most recent forecast on/before trade date)
    fc = res[["date", "fcast_vol_ann", "regime"]].sort_values("date")
    merged = pd.merge_asof(df.sort_values("date"), fc, on="date", direction="backward")
    merged = merged.dropna(subset=["fcast_vol_ann"]).copy()

    base = _stats(merged["profit_usd"].to_numpy(), capital)

    # 4) vol-targeted variants
    variants = []
    for tv in targets:
        mult = merged["fcast_vol_ann"].apply(lambda v: size_from_vol(v, tv)).to_numpy()
        vt = _stats(merged["profit_usd"].to_numpy() * mult, capital)
        vt.update({"target": tv, "avg_mult": float(mult.mean()),
                   "d_net": vt["net"] - base["net"],
                   "d_sharpe": vt["sharpe"] - base["sharpe"]})
        variants.append(vt)

    # regime breakdown of the fixed strategy
    regimes = []
    for rg, grp in merged.groupby("regime", observed=True):
        s = _stats(grp["profit_usd"].to_numpy(), capital)
        regimes.append({"regime": str(rg), "n": int(len(grp)), "net": s["net"],
                        "exp": s["exp"], "win_pct": float(100 * (grp["profit_usd"] > 0).mean())})

    best_sharpe_gain = max((v["d_sharpe"] for v in variants), default=0.0)
    verdict = _verdict(best_sharpe_gain, regimes)

    report = {
        "file": csv_path, "capital": capital, "price_source": price_source,
        "trades_total": int(len(df)), "trades_with_forecast": int(len(merged)),
        "forecast_days": int(len(res)),
        "regime_counts": {str(k): int(v) for k, v in
                          res.dropna(subset=["regime"])["regime"].value_counts().items()},
        "vol_ann": {"min": float(res.fcast_vol_ann.min()),
                    "median": float(res.fcast_vol_ann.median()),
                    "max": float(res.fcast_vol_ann.max())},
        "current": {"vol_ann": float(res.iloc[-1].fcast_vol_ann),
                    "regime": str(res.iloc[-1].regime)},
        "fixed": base, "vol_targeted": variants, "by_regime": regimes,
        "verdict": verdict,
    }
    _print(report)
    return report


def _verdict(best_sharpe_gain: float, regimes: list[dict]) -> str:
    storm = next((r for r in regimes if r["regime"] == "storm"), None)
    # low-vol reference: prefer 'calm', fall back to 'normal'
    low = next((r for r in regimes if r["regime"] == "calm"), None) \
        or next((r for r in regimes if r["regime"] == "normal"), None)
    tilt = ""
    if storm and low and storm["exp"] > low["exp"] * 1.5:
        tilt = (f" The strategy earns most in high-vol 'storm' regimes "
                f"(exp ${storm['exp']:.0f} vs ${low['exp']:.0f} in '{low['regime']}'), so "
                f"vol-targeting (which shrinks size when vol is high) would starve it "
                f"exactly when it works.")
    elif storm and low and low["exp"] > storm["exp"] * 1.5:
        tilt = (f" The strategy is hurt in high-vol 'storm' regimes "
                f"(exp ${storm['exp']:.0f} vs ${low['exp']:.0f} in '{low['regime']}'), so "
                f"vol-targeting has something real to fix — a good candidate for GARCH sizing.")
    if best_sharpe_gain < 0.02:
        return ("Vol-targeting does NOT improve this strategy — per-trade Sharpe is "
                "essentially unchanged, so it only rescales the P&L." + tilt)
    return (f"Vol-targeting improves per-trade Sharpe by up to {best_sharpe_gain:+.3f}." + tilt)


def _print(r: dict):
    print(f"\n{'='*64}\n  {r['file']}")
    print(f"  price source: {r['price_source']}")
    print(f"  {r['trades_with_forecast']} of {r['trades_total']} trades had a GARCH "
          f"forecast   |   {r['forecast_days']} forecast days")
    print(f"  regime days: {r['regime_counts']}")
    print(f"  forecast vol ann: min {r['vol_ann']['min']:.1f}  median "
          f"{r['vol_ann']['median']:.1f}  max {r['vol_ann']['max']:.1f}")
    print(f"  current: vol {r['current']['vol_ann']:.1f}%  regime {r['current']['regime']}\n{'='*64}")

    b = r["fixed"]
    print("\n-- FIXED (strategy as-traded, forecastable subset) --")
    print(f"  net ${b['net']:,.0f}   exp ${b['exp']:.1f}   per-trade Sharpe "
          f"{b['sharpe']:.3f}   maxDD {b['maxdd']:.2f}%")

    print("\n-- GARCH VOL-TARGETED (scale each trade by target/forecast, cap 0.25-2x) --")
    print(f"  {'target%':>7} {'avgMult':>8} {'net$':>12} {'exp$':>8} {'Sharpe':>7} "
          f"{'maxDD%':>7} {'dSharpe':>8} {'vsFixed$':>11}")
    for v in r["vol_targeted"]:
        print(f"  {v['target']:>7.0f} {v['avg_mult']:>8.2f} {v['net']:>12,.0f} "
              f"{v['exp']:>8.1f} {v['sharpe']:>7.3f} {v['maxdd']:>7.2f} "
              f"{v['d_sharpe']:>+8.3f} {v['d_net']:>+11,.0f}")

    print("\n-- FIXED STRATEGY P&L BY GARCH REGIME --")
    order = {"calm": 0, "normal": 1, "storm": 2}
    for rg in sorted(r["by_regime"], key=lambda x: order.get(x["regime"], 9)):
        print(f"  {rg['regime']:7} n={rg['n']:>3}  net ${rg['net']:>9,.0f}  "
              f"exp ${rg['exp']:>6.1f}  win% {rg['win_pct']:.0f}")

    print(f"\n  VERDICT: {r['verdict']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="TradingView List of Trades CSV/XLSX")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--targets", default="10,15,20",
                    help="comma-separated annualized vol targets %% (default 10,15,20)")
    ap.add_argument("--min-train", type=int, default=250,
                    help="days of history before the first forecast (default 250)")
    ap.add_argument("--regime-lookback", type=int, default=126,
                    help="window for vol percentile / regime (default 126 ~ 6mo)")
    ap.add_argument("--periods-per-year", type=int, default=252,
                    help="252 for index/futures (default), 365 for crypto")
    ap.add_argument("--prices", help="CSV of real daily bars (date,close) to use for "
                    "GARCH instead of the trade-price proxy — needed for low-frequency "
                    "(breakout/swing) strategies")
    ap.add_argument("--json", help="write full report JSON here")
    args = ap.parse_args()

    targets = [float(x) for x in args.targets.split(",") if x.strip()]
    report = review(args.export, args.capital, targets, args.min_train,
                    args.regime_lookback, args.periods_per_year, prices_csv=args.prices)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n  report JSON -> {args.json}")


if __name__ == "__main__":
    main()
