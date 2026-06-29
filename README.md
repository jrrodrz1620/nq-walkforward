# NQ Walk-Forward Analyzer

A Streamlit app + toolkit for validating futures strategies out-of-sample. Built
around the **Phantom Flow SMC** strategy but works with any TradingView
"List of Trades" export.

## Apps & scripts

| File | Run | What it does |
|------|-----|--------------|
| `app.py` | `streamlit run app.py` | Upload a TradingView export → folds, OOS equity, overfit check, **new metrics** (expectancy, R-multiple, payoff, Sharpe, streaks) and a **Monte Carlo** section. |
| `demo.py` | `python demo.py` | Runs the full analysis pipeline on a synthetic export — no TradingView/browser needed. |
| `run_backtest.py` | `python run_backtest.py` | Runs the **Phantom Flow SMC strategy logic in Python** over OHLC data, then walk-forward analyzes the real trades. |
| `sweep.py` | `python sweep.py` | Grid-searches key parameters and plots an **overfit map** (in-sample vs out-of-sample PF) so you keep robust settings, not in-sample winners. |
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

## Modules

- `dataio.py` — TradingView export loader (importable/testable).
- `metrics.py` — fold splitting, performance metrics, bootstrap Monte Carlo.
- `sample_data.py` / `backtest.py` — synthetic trade and OHLC generators.

## Install

```bash
pip install -r requirements.txt
```

## Strategy source

The Pine indicator and strategy live in the companion `pinescript-agents` repo
under `projects/phantom-flow-smc*.pine`.
