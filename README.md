# NQ Walk-Forward Analyzer

A Streamlit app + toolkit for validating futures strategies out-of-sample. Built
around the **Phantom Flow SMC** strategy but works with any TradingView
"List of Trades" export.

## Apps & scripts

| File | Run | What it does |
|------|-----|--------------|
| `app.py` | `streamlit run app.py` | **Three tabs:** (1) *Walk-Forward Analyzer* — upload a TradingView export → folds, OOS equity, overfit check, metrics (expectancy, R-multiple, payoff, Sharpe, streaks), Monte Carlo; (2) *Parameter Sweep* — interactive overfit map; (3) *Walk-Forward Optimization* — re-optimize per fold + stitched OOS equity. Tabs 2–3 run on synthetic bars or an uploaded OHLC CSV. |
| `demo.py` | `python demo.py` | Runs the full analysis pipeline on a synthetic export — no TradingView/browser needed. |
| `run_backtest.py` | `python run_backtest.py` | Runs the **Phantom Flow SMC strategy logic in Python** over OHLC data, then walk-forward analyzes the real trades. |
| `sweep.py` | `python sweep.py` | Grid-searches key parameters and plots an **overfit map** (in-sample vs out-of-sample PF) so you keep robust settings, not in-sample winners. |
| `wfo.py` | `python wfo.py` | **Walk-forward optimization**: re-optimizes params on each train window, trades them on the next OOS window, stitches the OOS equity. The optimism gap (train PF → OOS PF) is the real robustness signal. |
| `wf_analyze_export.py` | `python wf_analyze_export.py trades.csv` | **One-command audit of a real TradingView export**: overall metrics, walk-forward folds (train PF vs OOS test PF + optimism gap), Monte Carlo, profit concentration (is it one lucky trade?), slippage sensitivity (break-even pts/leg), and P&L by hour-of-day. `--chart eq.png` writes an equity+drawdown plot; `--point-value` sets $/pt/contract for slippage (CFD≈1, E-mini NQ=20). |
| `garch_size_review.py` | `python garch_size_review.py trades.csv` | **Does GARCH vol-targeting improve THIS strategy?** Builds a daily price series (real bars via `--prices daily.csv`, else a proxy from the export's own trade prices), fits walk-forward GARCH (via `skills/garch`), scales each trade's P&L by `target_vol/forecast_vol`, and compares fixed vs vol-targeted (net, expectancy, per-trade Sharpe, drawdown) plus a per-regime P&L breakdown and an automated verdict. Mean-reversion strategies that thrive in storms won't benefit; trend/breakout strategies hurt by high vol will. Use `--prices` for low-frequency (breakout/swing) strategies — the trade-price proxy is too sparse when trades are infrequent. |
| `test_backtest.py` | `python test_backtest.py` *or* `pytest` | Unit tests for the fill logic (target/stop/partial/breakeven) + run_backtest invariants. |

### Backtest harness

```bash
python run_backtest.py                  # synthetic OHLC (self-contained)
python run_backtest.py --csv bars.csv   # your own OHLC: time,open,high,low,close
python run_backtest.py --xlsx out.xlsx  # also save a TradingView-style export
```

`backtest.py` is a faithful Python port of the Pine strategy: confirmed swing
pivots → BOS/CHoCH → a structure shift **arms** a side → entry on the **pullback
into discount/premium** → structure/ATR stop → partial at 1R + breakeven → R:R
target. Intrabar fills are conservative (stop assumed first when a bar touches
both stop and target).

> On synthetic random-walk data the strategy is a slight net loser after
> commissions — which is correct: noise has no edge to capture. Point it at real
> NQ/MNQ bars with `--csv` to get meaningful numbers.

### Reading the overfit map (`sweep.py`)

Each point is a parameter combo: in-sample PF on the x-axis, out-of-sample PF on
the y-axis, colored by trade count. The dashed diagonal is "no degradation".
Combos that sit high on x but low on y (bottom-right) looked great in-sample and
fell apart out-of-sample — overfit, and usually on few trades. Prefer combos
that sit on/above the diagonal **with a healthy trade count**.

### WFO vs. the analyzer — what's the difference?

- **`app.py` / `demo.py`** walk-forward *analyze* one fixed strategy: they split a
  single trade list and check whether the same parameters hold up OOS.
- **`wfo.py`** walk-forward *optimize*: it re-picks parameters on each train
  window and trades them forward. This is the harder, more honest test — if the
  best train params keep changing and OOS lags train badly, the edge is fragile.

## Modules

- `dataio.py` — TradingView export loader (importable/testable).
- `metrics.py` — fold splitting, performance metrics, bootstrap Monte Carlo.
- `sample_data.py` / `backtest.py` — synthetic trade and OHLC generators.

## Install

```bash
pip install -r requirements.txt
```

## GARCH Method skill — position sizing

Vendored from the [`garchmethod`](https://github.com/milesdeutscher/garchmethod)
Claude Code plugin (MIT, © Miles Deutscher — see `skills/garch/LICENSE`). It
answers *how much* to size a position — never *which way* — via walk-forward
GARCH(1,1) volatility forecasting, and pairs naturally with the walk-forward
tooling above. The skill (`skills/garch/SKILL.md`) loads automatically in Claude
Code sessions for this repo; the scripts run standalone with
[`uv`](https://docs.astral.sh/uv/) — dependencies resolve from inline metadata,
nothing to install:

```bash
uv run scripts/garch_forecast.py --csv prices.csv --json   # 1-day vol forecast + regime
uv run scripts/vol_target.py     --csv prices.csv --target-vol 15 --json   # size multiplier
uv run scripts/compare.py        --csv prices.csv --chart equity.png       # fixed vs vol-targeted
```

`--ticker BTC-USD` works too (via yfinance). `garch-method.md` is the plugin's
original zero-trust onboarding prompt. Use `--periods-per-year 252` for equities
(the default 365 assumes crypto).

## Strategy source

The Pine indicator and strategy live in the companion `pinescript-agents` repo
under `projects/phantom-flow-smc*.pine`.
