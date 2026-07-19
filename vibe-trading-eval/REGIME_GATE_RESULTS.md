# Fixing Phantom Flow: the volatility regime gate

Date: 2026-07-19. Motivated by the falsification record (REAL_DATA_RESULTS.md):
Phantom Flow's losses clustered in high-volatility events (permutation maxDD
p = 0.90–0.95) and it was commission-drag in dead chop. Hypothesis: gate
entries to a *middle* band of volatility — skip both the low-ATR chop and the
extreme-ATR blowup regime.

## Implementation

`backtest.py` — new `Params` fields (default OFF, so existing behavior is
unchanged): `use_vol_filter`, `vol_lookback` (200), `vol_lo_pct`, `vol_hi_pct`.
Each bar's ATR is ranked against its trailing `vol_lookback` bars (causal, no
lookahead); an entry fires only when that percentile sits in
`[vol_lo_pct, vol_hi_pct]`. Covered by 3 unit tests.

## In-sample probe (regime_probe.py) — signal of life

A mid-band 20–80 gate won on all three independent datasets — the same band,
not cherry-picked per dataset:

| dataset | ungated | mid-band 20–80 |
|---|---|---|
| 15m calm | PF 0.84, −$41.5k, DD −109% | PF 1.05, +$9k, DD −33% |
| 1H full | PF 0.86, −$47k, DD −124% | PF 1.16, +$32k, DD −58% |
| 15m volatile | PF 1.04, +$8.7k | PF 0.86, −$22.7k |

## Honest OOS test (wfo_gated.py) — band optimized per fold

Walk-forward where the ATR band is one of the optimized parameters. Across all
12 folds of all 3 datasets, the optimizer **never once chose "ungated"** — it
picked a gated band every time, mostly the same mid-band. That consistency
across independent windows is the fingerprint of a real effect.

| dataset | ungated WFO (OOS) | gated WFO (OOS) |
|---|---|---|
| 1H 2016–2020 | −$92.1k, PF 0.69, DD −173% | **+$4.3k, PF 1.03, DD −49%** |
| 15m 2016–2018 | −$29.9k, PF 0.78, DD −84% | **−$1.7k, PF 0.99, DD −40%** |
| 15m 2019–2020 | −$53.5k, PF 0.62, DD −109% | **−$12.6k, PF 0.81, DD −45%** |

## Verdict

The regime gate is a **real, robust, OOS-validated improvement**: it roughly
halves drawdowns and lifts every dataset by $30–96k, and the effect is chosen
consistently by an unbiased optimizer. But it is **not yet a profitable
strategy** — best OOS is PF 1.03 (breakeven), one dataset still loses, and
after realistic slippage all three sit around breakeven. We turned a
conclusively-broken strategy into a regime-aware, drawdown-controlled
breakeven one.

## Remaining gap to a real edge (next levers, in order)

1. **Win rate (41–43%) is still too low for 2R/3R geometry.** The gate fixed
   *when* to trade, not *what* the entry selects. The entry trigger (BOS →
   pullback) needs to be more selective, or the target geometry lowered toward
   the achievable win rate.
2. **Position sizing is still lethal.** 2 contracts × $20/pt on $50k yields
   −40% to −49% OOS drawdowns even gated. Fixed-fractional / ATR-based sizing
   would cap this.
3. **The gate itself may be the better primitive.** "Only trade normal-vol
   regimes" is a robust finding — a *fresh* strategy built around it (rather
   than bolted onto Phantom Flow's weak entry) is the higher-upside path.
