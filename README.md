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
