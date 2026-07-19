# Hands-on evaluation: HKUDS/Vibe-Trading vs. nq-walkforward

Date: 2026-07-19 · Vibe-Trading v0.1.11 (`pip install vibe-trading-ai`)

Phantom Flow SMC was ported to Vibe-Trading's signal-engine contract and both
stacks were run on the **same 6,000 synthetic 15-min NQ bars**
(`backtest.generate_ohlc(n_bars=6000, seed=11)`, constant volume column added).

## Setup that worked

- `pip install vibe-trading-ai` into a clean Python 3.11 venv — clean install,
  no LLM/API key needed for the backtest library.
- Local data via `~/.vibe-trading/data-bridge/config.yaml` mapping symbol
  `NQ.CME` → the shared CSV (`columns: {date: time}`).
- Run dir: `config.json` + `code/signal_engine.py`
  (see `run_dir/` here), executed with
  `VIBE_TRADING_ALLOWED_RUN_ROOTS=<parent> python -m backtest.runner <run_dir>`.
- `NQ.CME` auto-routes to `GlobalFuturesEngine`: $20/pt multiplier,
  $2.25/side commission, integer contracts, 7% price-limit checks. NQ/MNQ are
  first-class in its product tables despite the docs never mentioning CME
  examples.
- `initial_cash: 72000` with default 10× leverage sizes weight ±1.0 to exactly
  2 contracts at ~18,000 — matching `Params.contracts = 2`.

## Parity result (the headline)

| Check | Result |
|---|---|
| Round-trip trades | **68 = 68** (identical) |
| Entry dates match (±1 day) | **68/68** |
| Directions match | **68/68** |
| Per-trade P&L correlation | **0.958** |
| Net P&L | −$13,380 (ours, A2) vs −$3,276 (Vibe) |

The signal port is trade-for-trade faithful. The net-P&L gap is entirely the
**fill model**: Vibe-Trading's engine is a portfolio-weight rebalancer that
executes at the **next bar's open** — it has no intrabar stop/target fills.
Our harness fills the stop *at the stop price* (conservatively, stop-first);
Vibe exits at the next open, which for breakeven stops scatters exits around
entry instead of pinning them at the stop. On this dataset that flattered the
strategy by **≈ +$149/trade (σ ≈ $614)**. Win rate diverges for the same
reason: 13% (exact BE stops = tiny losses after commission) vs 43% (next-open
exits go either way).

Both stacks agree on the conclusion that matters: the strategy is a net loser
on random-walk data (PF 0.61 ours / 0.80 theirs, negative Sharpe both), which
is the correct answer — noise has no edge.

## Validation stack comparison (same run)

| | nq-walkforward | Vibe-Trading |
|---|---|---|
| Walk-forward | 5 folds, train→OOS PF 0.69→0.44 (ratio 0.63) | 5 windows, 2/5 profitable, consistency 0.4 |
| Monte Carlo | bootstrap of trade P&L, P(loss) 91.8% | permutation test, Sharpe p=0.76 (not better than random ordering) |
| Extra | overfit map (`sweep.py`), per-fold **param re-optimization** (`wfo.py`) | bootstrap Sharpe CI [−0.56, 0.27], P(Sharpe>0)=0.24, benchmark/IR |

Both frameworks reject the strategy on this data — from different angles
(degradation ratio vs. permutation p-value / consistency rate). The
approaches are complementary, not redundant.

## Capability gaps found (relevant to this repo's workflow)

1. **No intrabar fills.** Signals are bar-close weights executed next-open.
   Stop/target/partial mechanics of an intraday futures strategy cannot be
   represented; exit prices are systematically mispriced. For stop-driven NQ
   scalping/day-trading, keep this repo's harness as the source of truth.
2. **No position resizing.** The engine only opens flat→position and closes on
   direction flip/zero — partial exits (50% at 1R) are inexpressible. The port
   runs with `use_partial=False` (compare Pipeline A2).
3. **No parameter walk-forward optimization.** Its `walk_forward` validation
   analyzes fixed-strategy consistency across windows (like `app.py`);
   its "optimizers" are portfolio weighters (risk parity, mean-variance…).
   Nothing does what `wfo.py` does (re-pick params per train window).
4. **No TradersPost/Tradovate/webhook execution** — but it exports strategies
   to Pine v6, which slots into the existing TradingView→TradersPost path.
5. **Intraday artifact wart:** `artifacts/trades.csv` truncates fill
   timestamps to dates (equity.csv keeps full 15-min timestamps).

## What it does well

- Data plumbing: symbol→loader routing with fallback chains, local CSV bridge,
  resampling, OHLC validation — all worked first try.
- Realistic futures frictions out of the box: per-product multipliers,
  commissions, slippage model, margin tables, price limits, integer contracts.
- The validation suite (permutation MC + bootstrap Sharpe CI + window
  consistency) is a solid, deterministic second opinion — usable as a plain
  Python library without any of the agent/LLM machinery.
- Security posture is real: AST-scrubbed strategy code, allowlisted run roots.

## Verdict

Use Vibe-Trading as a **research/validation second opinion and idea source**
(alpha zoo, permutation tests, Shadow Account), not as a replacement for this
repo's execution-accurate NQ backtesting. The highest-value, lowest-effort
borrow: add a **permutation Monte Carlo** (shuffle trade order, p-value on
Sharpe/maxDD) and a **bootstrap Sharpe CI** to `metrics.py` — both operate on
the trade list we already produce and need nothing from the agent platform.

## Files

- `make_data.py` — generates the shared dataset (paths assume the eval
  container layout: repo at `/home/user/nq-walkforward`, workspace at
  `/home/user/vibe-eval`).
- `run_pipeline_a.py` — this repo's stack, two variants (defaults; like-for-like).
- `run_dir/` — the exact Vibe-Trading run directory (config + signal engine).
- `data-bridge-config.yaml` — the `~/.vibe-trading/data-bridge/config.yaml` used.
- `results/pipeline_a_results.json` — our metrics/WF/MC output.
- `results/vibe_metrics.json` — Vibe-Trading headline metrics (runner stdout).
- `results/vibe_validation.json` — Vibe-Trading MC/bootstrap/walk-forward output.
