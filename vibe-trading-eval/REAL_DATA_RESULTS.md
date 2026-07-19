# Phantom Flow SMC on real Nasdaq-100 data — full falsification record

Date: 2026-07-19. Data: Oanda NAS100 CFD 1-minute bars
(github.com/FutureSharks/financial-data), resampled to 15m / 1H. A proxy for
NQ price action (index points, 24×5), not literal futures session data.
Rebuild the datasets with `build_real_datasets.py`. All runs: this repo's
`run_backtest.py` / `sweep.py` / `wfo.py`, $50k capital, 2 contracts × $20/pt.

## Matrix of results

| Test | 15m 2019–May 2020 (volatile) | 15m 2016–2018 (calm) | 1H 2016–May 2020 (full span) |
|---|---|---|---|
| Bars / trades | 31,674 / 344 | 70,125 / 760 | 25,716 / 290 |
| Baseline PF (net) | 1.04 (+$8.7k) | 0.84 (−$41.5k) | 0.86 (−$47.0k) |
| Baseline max DD | −86.8% | −109.5% | −124.6% |
| P(Sharpe>0), bootstrap | 55.7% | 5.3% | 22.1% |
| Permutation maxDD p | 0.95 (losses cluster) | 0.90 | 0.54 |
| Sweep: combos net > 0 | 4/30 (all overfit, OOS PF ≤ 0.65) | **0/30** | **0/30** |
| Sweep: best OOS PF | 1.01 (net −$28k) | 1.22 (IS 0.79 — luck) | 1.39 (IS 0.55 — luck) |
| WFO stitched OOS | −$53.5k, PF 0.62, DD −108.7% | −$29.9k, PF 0.78, DD −83.5% | −$92.1k, PF 0.69, DD −173.0% |

Plots: `plots/` (overfit maps + stitched WFO equity per test).

## Findings

1. **No edge at any tested configuration, timeframe, or regime.** 90 sweep
   combos across three datasets: zero were profitable in-sample AND
   out-of-sample. WFO (honest re-optimization) lost money on all three.
2. **Calm regimes don't rescue it.** 2016–2018: every combo lost in-sample;
   the calmest WFO fold (Aug 2017–Feb 2018) was the best result at exactly
   breakeven (PF 1.00). The strategy is commission drag in chop.
3. **Volatility is what kills it.** Loss clustering (permutation maxDD
   p = 0.90–0.95) concentrates in vol events: Volmageddon fold PF 0.48
   (−$20k), COVID folds −$40k (15m) and −$71.7k (1H).
4. **Higher timeframe makes it worse, not better.** 1H widens structure stops
   while size stays 2 contracts, so each loss is larger: expectancy
   −$162/trade vs −$55 (15m calm), and the worst account path (−173% DD).
5. **Win rate is structurally too low for the geometry.** 33–40% realized
   across all runs; 2R targets with partial-at-1R need better than that
   after commissions.

## Verdict

The BOS → pullback entry logic as ported has no exploitable edge on
Nasdaq-100 intraday data 2016–2020. This is a signal-logic falsification,
not a tuning problem. Before any live deployment, reconcile these fills
against the TradingView Pine results on the same period — if Pine showed
profits, the divergence is in fill assumptions (this harness fills stops
conservatively; TV strategies often don't).
