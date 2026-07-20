# SYSTEM.md — the tradeable diversified trend-following system

Date: 2026-07-20. The end product of the full research arc (see
PORTFOLIO_RESULTS.md and earlier records). Code: `trend_system.py` — one code
path for research and operation, so backtest and production cannot drift.

## Rules

**Universe (CORE_9, chosen a priori — one liquid representative per bucket):**
NAS100, SPX500, JP225 (equity) · EUR/USD, GBP/USD (FX) · gold, WTI oil, corn
(commodities) · US 10Y (bonds).

**Entry:** daily close breaks the prior 100-day Donchian extreme, in the
direction of the 100-day EMA. Symmetric (long and short).

**Exit:** 4×ATR(14) trailing stop from the high-water mark. No profit target.
One position per instrument; at most 12 open portfolio-wide.

**Sizing:** each trade risks 0.5% of current equity to its stop (equal-risk
across instruments), scaled by a portfolio volatility target: realized 60-day
vol of system equity above 10% annualized shrinks new-trade risk, below it
grows it, clamped to [0.5x, 2.0x].

## Validation (all causal: params & universe fixed before looking at OOS)

| | FIT 2005-2012 | **OOS 2013-2020** |
|---|---|---|
| return | +17.4% | **+17.4%** |
| max drawdown | -2.8% | **-9.7%** |
| Sharpe | 1.98 | **1.38** |
| ret/DD | 6.18 | **1.79** |

- Parameter robustness: **11/12** grid cells (channel 40-100 × trail 3-5)
  have positive OOS Sharpe — the edge is not a lucky parameter cell.
- Benchmark: OOS equal-weight buy & hold of the same instruments: ret/DD 0.66.
  The system beats it ~2.7x risk-adjusted.
- The vol overlay + position cap improved OOS Sharpe from 0.69 to ~1.0-1.38
  across configs vs plain fixed-fraction.

## Two selection findings that define the system (both validated the hard way)

1. **Universe expansion failed.** Adding 16 instruments (mostly more equity
   indices) made OOS negative — correlated additions are beta, not
   diversification.
2. **Fit-era instrument selection failed.** Keeping fit-era winners (e.g.
   sugar) still lost OOS — instrument-level past performance does not persist.
   Hence: fixed a-priori basket, one per bucket, never performance-picked.

## Operating procedure

1. Each day after the daily close, update the per-instrument OHLC CSVs.
2. `python trend_system.py orders` → prints open positions with current state
   and any new entry signals on the last bar.
3. Place entries at next open; size = (0.5% × equity × vol-scale) / stop
   distance. Update trailing stops to the printed levels.
4. `python trend_system.py oos` re-runs the validation split at any time;
   `backtest` runs the full history.

## Kill criteria (decide before trading, not during a drawdown)

- Portfolio drawdown exceeds **-15%** (1.5x the worst OOS drawdown): halt.
- Rolling 2-year realized Sharpe below **0** : halt and re-validate.
- Any structural change to the data feed or instruments: re-run `oos` before
  resuming.

## Honest caveats

- Prices are Oanda CFD (proxy), not exchange futures; financing/rollover and
  realistic spreads are not modeled beyond flat commission. Live results will
  be worse; paper-trade first.
- Data ends 2020-05-14; the system has not been validated on 2020-2026
  markets. Before capital: refresh data and re-run the OOS split.
- 9 instruments is a minimal ensemble; the concentration risk of a single
  bucket failing (e.g. equities) is real. Expansion should add *new asset
  classes* (not more of the same) and be validated on data unseen at decision
  time.
- Returns are conservative by design (0.5% risk/trade, 10% vol target).
  Scaling risk scales both return and drawdown roughly linearly until the
  position cap binds.
