# Phase 2: fresh mean-reversion strategy — falsified by the gauntlet

Date: 2026-07-19. Built a with-trend mean-reversion strategy (fade a stretch
from the mean back toward it, only in the slow-trend direction, gated to the
normal-vol band, risk-sized) to attack Phantom Flow's diagnosed weakness (weak
entry / low win rate). `mr_strategy.py`.

## The trap it walked into (and why the gauntlet exists)

At untuned defaults the strategy looked like a breakthrough: net-positive on
all three datasets with 3-5x smaller drawdowns than the squeezed Phantom Flow,
including the 15m-volatile data PF never handled:

| dataset | MR with-trend (in-sample, defaults) |
|---|---|
| 1H full | +$508, PF 1.01, WR 48%, DD -12.4% |
| 15m calm | +$1,899, PF 1.02, WR 47%, DD -20.9% |
| 15m volatile | +$4,188, PF 1.09, WR 48%, DD -12.1% |

**But the net-positive dollars were inside the noise.** The significance tests
say so directly — bootstrap Sharpe CI straddles zero on every dataset:

| dataset | Sharpe 95% CI | P(Sharpe>0) | permutation maxDD p |
|---|---|---|---|
| 1H full | [-0.18, 0.17] | 54% | 0.32 |
| 15m calm | [-0.10, 0.11] | 57% | 0.66 |
| 15m volatile | [-0.11, 0.19] | 68% | 0.41 |

## OOS walk-forward (entry_z x stop_atr optimized per fold)

| dataset | stitched OOS net | PF | WR | DD |
|---|---|---|---|---|
| 1H full | +$1,036 | 1.05 | 43% | -16.4% |
| 15m calm | -$9,804 | 0.80 | 40% | -31.1% |
| 15m volatile | -$7,821 | 0.76 | 45% | -23.2% |

Two of three lose out-of-sample; the one that "wins" is +$1k over 4.4 years —
economically negligible. Per-fold parameters are unstable and several folds
trade only 2-11 times (PF 2.54, inf are small-sample noise), the signature of
curve-fitting.

## Verdict

The mean-reversion strategy is **not a real edge**. In-sample profitability was
statistically indistinguishable from zero, and it does not survive OOS. The
lesson is methodological and worth keeping: *lead with the significance test,
not the net-P&L number* — the bootstrap CI flagged this as a coin flip before
the walk-forward confirmed it.

## Where the two phases leave us

- **Most robust finding of the whole investigation:** the normal-vol regime
  gate. It roughly halves drawdowns and is chosen consistently by an unbiased
  optimizer across regimes and timeframes — real, if not by itself profitable.
- **Best actual strategy:** the squeezed Phantom Flow on 1H (PF ~1.05 OOS,
  DD ~-32%) — thin and marginal.
- **No configuration tested clears a convincing, significant, tradeable edge**
  on NAS100 intraday 2016-2020. The honest state is: a robust risk-reducing
  regime filter, and two signals (breakout, mean-reversion) that are each at
  best breakeven and at worst noise.
