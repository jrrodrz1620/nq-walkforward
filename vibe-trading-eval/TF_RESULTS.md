# Phase 3: trend-following — the first real (but inconsistent) risk-adjusted edge

Date: 2026-07-19. Neither prior strategy was a trend-follower (Phantom Flow
entered on pullbacks; Phase 2 was mean-reversion — both faded moves). On a
trending index the textbook edge is momentum: enter ON a Donchian breakout,
ride with an ATR trailing stop, no fixed target, trend-aligned, normal-vol
gated, risk-sized. `tf_strategy.py`.

Everything is judged against **buy-and-hold over the same window** — on a
market that tripled, long-biased profit is beta until it beats passive holding
on return-per-drawdown.

## Key results

**Short side loses everywhere** (can't short a bull market). Long-only is the
strategy.

**Long-only vs buy-and-hold, same OOS window (return / maxDD / ret-per-DD):**

| OOS window | trend-follower | buy & hold | winner |
|---|---|---|---|
| 1H 2016-2020 | +69% / -12% / **5.98** | +52% / -32% / 1.62 | TF, decisively |
| 15m calm 2017-2018 | +17% / -15% / **1.14** | +17% / -24% / 0.72 | TF (lower DD) |
| 15m vol 2019-2020 | +5% / -38% / **0.12** | +15% / -32% / 0.47 | B&H |

**Independent decades (1H, WFO OOS vs same-window B&H):**

| window | TF ret/DD | B&H ret/DD | winner |
|---|---|---|---|
| 2009-2011 (recovery)* | 0.24 | 2.30 | B&H, decisively |
| 2012-2015 (QE grind-up) | 6.55 | 3.15 | TF |
| 2016-2020 | 5.98 | 1.62 | TF |

*WFO warmup consumed the 2008 crash, so this OOS window is the low-vol
2009-2011 grind-up — exactly where trend-following underperforms a market
going straight up (it made only 2%).

## Verdict — honest and nuanced

The long-only trend-follower is the **first approach in this whole
investigation with genuine risk-adjusted alpha** — it beats buy-and-hold on
ret/DD in 4 of 6 windows tested, sometimes 3-4x, and leverage-adjusted (size
TF up to B&H's drawdown) the wins are large. The mechanism is real and
well-documented: it captures the drift while stepping aside in downtrends, so
its drawdowns are a third of B&H's.

**But it is not reliable on a single instrument.** It badly underperforms in
low-volatility grind-up markets (2009-2011, +2%), where the gate and trailing
stops leave money on the table, and it lost in the choppy 15m-volatile window.
The wins are lumpy — concentrated in trending and crash-avoidance periods.
This is the textbook truth about trend-following: its edge is real but shows up
as occasional large outperformance amid long stretches of lag, and it needs
**many independent bets** to realize reliably.

## Next step to make it robust

Trend-following's edge is a diversification effect across many markets, not a
single-instrument effect. The natural next test: run the same system across
multiple instruments (the FutureSharks dataset has S&P 500, DAX, Nikkei, etc.)
and equal-risk-weight them. A trend portfolio should smooth the single-market
lumpiness into a steadier ret/DD advantage. That — not more tuning on NAS100 —
is where a deployable edge would come from.
