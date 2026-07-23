---
name: garch-method
description: Volatility forecasting and position sizing via walk-forward GARCH(1,1). Use whenever the user asks about volatility forecasts, position sizing, "how much should I put on", vol targeting, risk throttling, storm/calm regimes, or wants to test whether vol-targeted sizing improves an existing strategy. Works on any ticker (yfinance) or any CSV with date + close columns. Answers "how much" — never "which way".
---

# GARCH Method — volatility forecasting + position sizing

This skill answers the question retail never asks and every fund asks daily: **how much?**

It does NOT predict direction. GARCH forecasts the *magnitude* of moves — how violent tomorrow is likely to be, not which way it goes. Say this to the user whenever presenting results.

## The three tools

All scripts live in `scripts/` and run with `uv run` (dependencies resolve automatically via inline metadata — nothing to pip-install).

### 1. `garch_forecast.py` — the forecast
Walk-forward GARCH(1,1), zero lookahead (params re-estimated every 21 days on an expanding window; the recursion rolls forward between refits using only past data).

```
uv run scripts/garch_forecast.py --csv prices.csv --json
uv run scripts/garch_forecast.py --ticker BTC-USD --json
```

Output: 1-day-ahead vol forecast (daily + annualized), vol percentile vs trailing year, regime (calm / normal / storm).

### 2. `vol_target.py` — the size
The entire idea: `size = target_vol / forecast_vol`, capped at [0.25x, 2.0x].

```
uv run scripts/vol_target.py --csv prices.csv --target-vol 15 --json
```

Output: position size multiplier. "Run 0.6x your baseline" — that's the answer.

### 3. `compare.py` — the honest test
Runs the same signals twice — fixed size vs vol-targeted — and shows both equity curves plus stats side by side. Ships with an EMA 9/21 crossover demo; accepts any strategy via `--signals mine.csv` (columns: date, signal in {-1,0,1}).

```
uv run scripts/compare.py --csv prices.csv --target-vol 58 --chart equity.png --json
uv run scripts/compare.py --csv prices.csv --signals mine.csv
```

Output: CAGR, ann vol, Sharpe, max drawdown, worst month, final equity — both versions — plus the equity-curve chart with storm regimes shaded.

## JSON contract

Every script supports `--json`. Core output shape:

```json
{
  "as_of": "2026-05-23",
  "forecast_vol_annualized_pct": 41.2,
  "vol_percentile_1y": 78.0,
  "regime": "storm",
  "position_size_multiplier": 0.6,
  "note": "GARCH forecasts magnitude (volatility), not direction."
}
```

## Three composition patterns

**A. Sizing layer** — bolt onto any existing strategy. Your strategy decides *if*; this skill decides *how much*. Take the strategy's signal, multiply by `position_size_multiplier`, done.

**B. Risk throttle** — standalone kill-switch. If `regime == "storm"`, cut all exposure to the multiplier regardless of what your signals say. Works with any agent that manages positions.

**C. Comparison harness** — before trusting any strategy, run it through `compare.py` and check whether vol targeting improves its Sharpe / drawdown. If sizing doesn't help, the strategy's edge may be too weak to survive real conditions.

Composes cleanly with regime-direction skills (e.g. Markov-style bull/bear classifiers): their output answers *which way*, this answers *how much*. Multiply the two.

## Defaults & conventions

- Crypto: `--periods-per-year 365` (default). Stocks: `--periods-per-year 252`.
- Target vol: 15% is a sane conservative default. For a risk-matched comparison against an unlevered strategy, set target approximately equal to the strategy's own realized vol.
- Data: yfinance ticker (needs internet) or any CSV with date + close columns — drops into whatever pipeline the user already runs.
- Minimum history: ~510 daily observations before the first forecast.

## Honesty rules (non-negotiable)

1. Never present GARCH output as a direction call.
2. Never hide the drawdown or worst-month numbers when reporting a comparison.
3. If vol targeting does NOT improve the user's strategy, say so plainly — that result is just as valuable.
