# Phase 1: squeezing the gated Phantom Flow to its ceiling

Date: 2026-07-19. After the regime gate got the strategy to breakeven
(REGIME_GATE_RESULTS.md), Phase 1 fixed its two remaining weaknesses —
position sizing and target geometry — to find the ceiling of the old signal.

## Changes

`backtest.py` — risk-based position sizing (default off): `size_mode="risk"`,
`risk_dollars`, `max_contracts`. Each trade is sized so a stop-out costs
~`risk_dollars`, capping per-trade loss and auto-scaling down when stops are
wide. Covered by 3 unit tests (15 backtest tests total pass).

## What the probe taught us (squeeze_probe.py, in-sample, gate 20-80)

1. **Risk sizing is a drawdown fix only when it sizes *down*.** On 1H (wide
   stops) it nearly halved DD (-57.6% -> -33.8%) at the same PF. On 15m (tight
   stops) "constant $ risk" sizes *up* and amplified losses — so it's
   timeframe-dependent, not free.
2. **Lowering target geometry backfired.** Win rate stayed flat (46/43/34%)
   regardless of rr, because partial-at-1R + breakeven decide most exits at 1R,
   not the final target. Lowering the target only shrinks winners. Higher rr is
   strictly better; rr 2.0 wins everywhere.
3. **The strategy has one home: 1H.** Profitable across *every* config on 1H
   (PF 1.02-1.16); marginal on 15m calm; a loser on 15m volatile no matter what.

## OOS ceiling (squeeze_wfo.py, band optimized per fold, 1H)

| config | stitched OOS net | PF | WR | max DD |
|---|---|---|---|---|
| gate + rr2.0, fixed 2ct | +$5,760 | 1.04 | 43% | -58.1% |
| gate + rr2.0, risk $500 | +$3,796 | 1.05 | 45% | **-32.5%** |

Bands chosen per fold were consistently gated (20-80 / 30-70), never
"ungated" — the regime effect stays robust with sizing added.

## Verdict

The ceiling of the fixed-up Phantom Flow is a **thin but genuine OOS edge on
1H**: PF ~1.05, positive net, drawdown controlled to ~-32% with risk sizing.
It is marginal because the *entry* (BOS -> pullback, 43-45% WR) is only just
over the ~40% break-even line for 2R geometry — sizing and geometry can't
create selectivity the signal lacks. This motivates Phase 2: a fresh strategy
built on the robust normal-vol-regime primitive with a more selective entry.
