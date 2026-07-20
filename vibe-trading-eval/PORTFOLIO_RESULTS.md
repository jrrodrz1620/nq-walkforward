# Phase 4: the diversified trend portfolio — a real, OOS-validated edge

Date: 2026-07-20. TF_RESULTS.md ended with: trend-following's edge is a
*diversification* effect across many markets, not a single-instrument one.
This tests that directly on 9 instruments across 4 asset classes (equity
indices, FX, metals/energy, ag, bonds): NAS100, SPX500, JP225, EUR_USD,
GBP_USD, XAU (gold), WTI (oil), CORN, US10Y. Symmetric TF (long AND short —
FX/commodities/bonds trend both ways), equal-risk-weighted on an R-multiple
basis (0.5% of capital risked per trade), benchmarked against equal-weight
buy & hold.

## The timeframe correction that mattered

**1H bars: total failure.** Symmetric TF on 1H lost on 8 of 9 instruments
(win rates 34-41%) — the portfolio was deeply negative. 1H breakouts are
dominated by noise and whipsaw; the earlier long-only NAS100 1H "win" was
largely beta + crash-avoidance on one trending index, not portable alpha.

**Daily bars: the edge appears.** Classic Donchian-55 breakout, wide ATR
trailing stop, vol gate OFF (trends come with expanding vol), 2005-2020:

| Daily 2005-2020 (in-sample, fixed a-priori params) | ret | maxDD | Sharpe | ret/DD |
|---|---|---|---|---|
| Trend portfolio | +19.5% | -8.6% | 0.93 | 2.28 |
| Equal-weight buy & hold | +93.7% | -51.9% | — | 1.81 |

6 of 9 instruments positive; diversification cut portfolio drawdown to -8.6%
(vs -51.9% for buy & hold) — the single-market lumpiness smoothed out exactly
as predicted.

## Out-of-sample confirmation (the honest test)

Optimize (channel_len, trail_atr) for best portfolio Sharpe on **2005-2012**,
apply UNCHANGED to **2013-2020**:

| | ret | maxDD | Sharpe | ret/DD |
|---|---|---|---|---|
| in-sample 2005-2012 | +16.9% | -2.2% | 2.45 | 7.64 |
| **out-of-sample 2013-2020** | **+10.9%** | **-8.3%** | **1.26** | **1.32** |
| OOS equal-weight buy & hold | +26.2% | -39.7% | — | 0.66 |

Out-of-sample **Sharpe 1.26** at **-8.3% max drawdown** — deployable-quality
trend-following, in the range of real managed-futures funds. It beat buy & hold
2:1 on risk-adjusted return with a ~5x smaller drawdown. The in-sample -> OOS
degradation (Sharpe 2.45 -> 1.26) is healthy, not a collapse. Equity curve:
`plots/portfolio_daily_equity.png` — a steady 15-year climb that rises through
2008 and spikes at the 2020 COVID crash (trend-following crisis alpha).

Lower absolute return than buy & hold is purely conservative 0.5%/trade sizing;
leverage-matched to B&H's -40% drawdown, the portfolio out-returns it.

## Verdict — the payoff of the whole investigation

This is the first, and only, approach tested that produces a **real,
out-of-sample-validated risk-adjusted edge**. The path that got here is the
point: every wrong turn was killed by the same gauntlet.

1. Phantom Flow (breakout-pullback) — falsified across regimes/timeframes.
2. Regime gate — real drawdown reducer, but breakeven alone.
3. Mean-reversion — insignificant in-sample, failed OOS.
4. Single-instrument trend-following — mostly beta; lumpy, failed the
   multi-decade test on its own.
5. **Diversified daily trend portfolio — OOS Sharpe 1.26, the genuine edge.**

The two decisive tools were the **buy-and-hold benchmark** (which unmasked
single-market beta as fake alpha) and **out-of-sample validation** (which
killed the mean-reversion mirage and confirmed the portfolio).

## Caveats before any capital

- Data is Oanda CFD prices (a proxy), not exchange futures; spreads/rollover
  not modeled beyond a flat commission.
- 9 instruments is a small ensemble; real trend funds run 50-100+.
- Daily bars only; no intraday execution assumptions tested.
- The next step toward deployment is a larger instrument universe and a
  volatility-targeted (not fixed-fraction) sizing overlay.
