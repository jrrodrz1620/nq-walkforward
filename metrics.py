"""
Shared performance-metric helpers for the Walk-Forward Analyzer.

Used by both the Streamlit app (app.py) and the CLI demo (demo.py) so the
calculations live in exactly one place.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────
# FOLD SPLITTING
# ─────────────────────────────────────────────

def split_folds(df: pd.DataFrame, n: int, train_pct: float, min_trades: int):
    """Split trades into sequential walk-forward folds (train then test)."""
    total = len(df)
    fold_size = total // n
    folds = []
    for i in range(n):
        start = i * fold_size
        end = start + fold_size if i < n - 1 else total
        chunk = df.iloc[start:end].copy()
        split = int(len(chunk) * train_pct / 100)
        train = chunk.iloc[:split]
        test = chunk.iloc[split:]

        if len(train) >= min_trades and len(test) >= 1:
            folds.append({
                "fold": i + 1,
                "train": train,
                "test": test,
                "train_start": train["entry_time"].iloc[0],
                "train_end": train["entry_time"].iloc[-1],
                "test_start": test["entry_time"].iloc[0],
                "test_end": test["entry_time"].iloc[-1],
            })
    return folds


# ─────────────────────────────────────────────
# STREAK HELPERS
# ─────────────────────────────────────────────

def _max_streak(mask: np.ndarray) -> int:
    """Longest run of True values in a boolean array."""
    best = run = 0
    for v in mask:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


# ─────────────────────────────────────────────
# CORE METRICS
# ─────────────────────────────────────────────

def calc_metrics(trades: pd.DataFrame, capital: float) -> dict:
    """Performance metrics for a set of trades.

    Adds expectancy, payoff ratio, per-trade Sharpe, R-multiple stats and
    win/loss streaks on top of the original net/win-rate/PF/drawdown set.
    """
    empty = {
        "n_trades": 0, "net_profit": 0, "win_rate": 0,
        "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
        "max_dd": 0, "return_pct": 0, "expectancy": 0,
        "payoff": 0, "sharpe": 0, "avg_r": 0, "total_r": 0,
        "max_win_streak": 0, "max_loss_streak": 0,
        "equity": np.array([capital]),
    }
    if len(trades) == 0:
        return empty

    pnl = trades["profit_usd"].to_numpy(dtype=float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    net_profit = pnl.sum()
    win_rate = len(wins) / len(pnl) * 100 if len(pnl) > 0 else 0
    avg_win = wins.mean() if len(wins) > 0 else 0.0
    avg_loss = losses.mean() if len(losses) > 0 else 0.0
    profit_factor = abs(wins.sum() / losses.sum()) if losses.sum() != 0 else np.inf

    # Expectancy: average $ outcome per trade.
    expectancy = pnl.mean()
    # Payoff ratio: average win size vs average loss size.
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else np.inf

    # Per-trade Sharpe (mean / std of trade PnL). Not annualized — trade-based.
    sharpe = pnl.mean() / pnl.std(ddof=1) if len(pnl) > 1 and pnl.std(ddof=1) > 0 else 0.0

    # R-multiples: express each trade in units of the average loss (= 1R).
    r_unit = abs(avg_loss) if avg_loss != 0 else np.nan
    if r_unit and not np.isnan(r_unit):
        r_mult = pnl / r_unit
        avg_r = float(r_mult.mean())
        total_r = float(r_mult.sum())
    else:
        avg_r = total_r = 0.0

    equity = capital + np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak * 100
    max_dd = dd.min()

    return {
        "n_trades": len(pnl),
        "net_profit": net_profit,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_dd": max_dd,
        "equity": equity,
        "return_pct": net_profit / capital * 100,
        "expectancy": expectancy,
        "payoff": payoff,
        "sharpe": sharpe,
        "avg_r": avg_r,
        "total_r": total_r,
        "max_win_streak": _max_streak(pnl > 0),
        "max_loss_streak": _max_streak(pnl < 0),
    }


# ─────────────────────────────────────────────
# MONTE CARLO
# ─────────────────────────────────────────────

def monte_carlo(pnl: np.ndarray, capital: float, n_sims: int = 1000,
                seed: int | None = 42) -> dict:
    """Bootstrap-resample the trades `n_sims` times to map the outcome range.

    Each simulation draws `len(pnl)` trades *with replacement* from the trade
    distribution. Unlike a pure order shuffle (whose final equity is fixed,
    since a sum is order-independent), resampling varies both the path and the
    final equity — so the probability of ending below starting capital is
    meaningful. Returns percentile bands for final equity and max drawdown.
    """
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) == 0:
        return {}

    rng = np.random.default_rng(seed)
    finals = np.empty(n_sims)
    max_dds = np.empty(n_sims)
    n = len(pnl)

    for i in range(n_sims):
        sample = rng.choice(pnl, size=n, replace=True)
        equity = capital + np.cumsum(sample)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak * 100
        finals[i] = equity[-1]
        max_dds[i] = dd.min()

    return {
        "final_p5": float(np.percentile(finals, 5)),
        "final_p50": float(np.percentile(finals, 50)),
        "final_p95": float(np.percentile(finals, 95)),
        "maxdd_p5": float(np.percentile(max_dds, 5)),
        "maxdd_p50": float(np.percentile(max_dds, 50)),
        "maxdd_p95": float(np.percentile(max_dds, 95)),
        "prob_loss": float((finals < capital).mean() * 100),
        "finals": finals,
        "max_dds": max_dds,
    }
