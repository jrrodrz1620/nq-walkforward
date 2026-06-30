"""
Python port of the Phantom Flow SMC strategy — a bar-by-bar backtest harness.

This mirrors the Pine strategy (projects/phantom-flow-smc-strategy.pine in the
pinescript-agents repo) closely enough to validate the *logic* without
TradingView: confirmed swing pivots → BOS/CHoCH → discount/premium entry →
structure/ATR stop → partial at 1R + breakeven → R:R target.

Output is a trades DataFrame in the analyzer's internal schema, so it flows
straight into metrics.split_folds / calc_metrics / monte_carlo.

Intrabar fills are approximated conservatively: when a bar touches both the
stop and a target, the stop is assumed to fill first.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────
# PARAMETERS  (defaults match the Pine strategy)
# ─────────────────────────────────────────────

@dataclass
class Params:
    swing_length: int = 10
    atr_length: int = 14
    stop_mode: str = "structure"   # "structure" | "atr"
    atr_mult: float = 1.5
    struct_buf: float = 0.25        # × ATR
    rr_ratio: float = 2.0
    use_location: bool = True       # require discount/premium
    use_trend: bool = True          # EMA trend filter (HTF proxy)
    trend_ema: int = 50
    use_partial: bool = True
    partial_pct: float = 50.0
    use_be: bool = True
    be_trigger: float = 1.0         # in R
    contracts: int = 2
    multiplier: float = 20.0        # NQ=20, MNQ=2, ES=50, MES=5
    commission: float = 2.0         # $ per contract per side


# ─────────────────────────────────────────────
# SYNTHETIC OHLC  (so the harness runs with no data feed)
# ─────────────────────────────────────────────

def generate_ohlc(n_bars: int = 4000, seed: int = 11, start_price: float = 18000.0,
                  tf_minutes: int = 15, drift: float = 0.02,
                  vol: float = 6.0) -> pd.DataFrame:
    """Random-walk OHLC bars with a mild regime-shifting drift.

    Not a market model — just plausible price action to exercise the strategy.
    """
    rng = np.random.default_rng(seed)
    # Drift flips sign partway through to create trend then chop.
    regime = np.where(np.arange(n_bars) < n_bars * 0.6, drift, -drift * 0.5)
    rets = rng.normal(regime, vol, n_bars)
    close = start_price + np.cumsum(rets)
    open_ = np.empty(n_bars)
    open_[0] = start_price
    open_[1:] = close[:-1]
    wick = np.abs(rng.normal(0, vol * 0.8, n_bars))
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.8, n_bars))
    times = pd.date_range("2024-01-02 09:30", periods=n_bars, freq=f"{tf_minutes}min")
    return pd.DataFrame({"time": times, "open": open_, "high": high,
                         "low": low, "close": close})


def load_ohlc_csv(path: str) -> pd.DataFrame:
    """Load an OHLC CSV with flexible column names (time/date, o/h/l/c)."""
    raw = pd.read_csv(path)
    cols = {c.lower().strip(): c for c in raw.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        raise KeyError(f"none of {names} found in {list(raw.columns)}")

    out = pd.DataFrame({
        "time": pd.to_datetime(raw[pick("time", "date", "datetime", "date/time")]),
        "open": pd.to_numeric(raw[pick("open", "o")], errors="coerce"),
        "high": pd.to_numeric(raw[pick("high", "h")], errors="coerce"),
        "low": pd.to_numeric(raw[pick("low", "l")], errors="coerce"),
        "close": pd.to_numeric(raw[pick("close", "c")], errors="coerce"),
    }).dropna().sort_values("time").reset_index(drop=True)
    return out


# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────

def _atr(df: pd.DataFrame, length: int) -> np.ndarray:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    prev_c = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    # Wilder's smoothing (RMA)
    atr = np.empty_like(tr)
    atr[0] = tr[0]
    a = 1.0 / length
    for i in range(1, len(tr)):
        atr[i] = atr[i - 1] + a * (tr[i] - atr[i - 1])
    return atr


def _ema(arr: np.ndarray, length: int) -> np.ndarray:
    out = np.empty_like(arr)
    out[0] = arr[0]
    k = 2.0 / (length + 1)
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


# ─────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────

def simulate_trade(direction: int, entry_i: int, entry_px: float, init_risk: float,
                   h: np.ndarray, lo: np.ndarray, c: np.ndarray, p: Params) -> dict:
    """Walk a single trade forward to its exit and return the realized result.

    Pure and self-contained (no structure logic), so the fill rules can be
    unit-tested directly. `direction` is +1 long / -1 short. Conservative
    intrabar assumption: if a bar touches both the stop and a target, the stop
    fills first. Returns {exit_i, exit_px, profit_usd}.

    Commissions: charged per contract on entry, and per contract on each exit
    leg (partial and final).
    """
    n = len(c)
    work_stop = entry_px - init_risk if direction > 0 else entry_px + init_risk
    open_contracts = float(p.contracts)
    partial_done = False
    realized = -p.contracts * p.commission          # entry commission, all contracts
    tp1 = entry_px + direction * init_risk
    tp_final = entry_px + direction * init_risk * p.rr_ratio

    def _finish(exit_px, exit_i):
        gross = (exit_px - entry_px) * direction * open_contracts * p.multiplier
        total = realized + gross - open_contracts * p.commission   # exit commission
        return {"exit_i": exit_i, "exit_px": float(exit_px), "profit_usd": round(total, 2)}

    for j in range(entry_i + 1, n):
        if direction > 0:
            if p.use_be and not partial_done and h[j] >= entry_px + init_risk * p.be_trigger:
                work_stop = max(work_stop, entry_px)
            if lo[j] <= work_stop:
                return _finish(work_stop, j)
            if not partial_done and p.use_partial and h[j] >= tp1:
                part = open_contracts * (p.partial_pct / 100.0)
                realized += (tp1 - entry_px) * part * p.multiplier - part * p.commission
                open_contracts -= part
                partial_done = True
                work_stop = max(work_stop, entry_px)            # breakeven on runner
            if h[j] >= tp_final:
                return _finish(tp_final, j)
        else:
            if p.use_be and not partial_done and lo[j] <= entry_px - init_risk * p.be_trigger:
                work_stop = min(work_stop, entry_px)
            if h[j] >= work_stop:
                return _finish(work_stop, j)
            if not partial_done and p.use_partial and lo[j] <= tp1:
                part = open_contracts * (p.partial_pct / 100.0)
                realized += (entry_px - tp1) * part * p.multiplier - part * p.commission
                open_contracts -= part
                partial_done = True
                work_stop = min(work_stop, entry_px)
            if lo[j] <= tp_final:
                return _finish(tp_final, j)

    # Ran out of data — close at the last bar's close.
    return _finish(c[n - 1], n - 1)


def run_backtest(ohlc: pd.DataFrame, p: Params = Params()) -> pd.DataFrame:
    df = ohlc.reset_index(drop=True)
    n = len(df)
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy(); c = df["close"].to_numpy()
    t = df["time"].to_numpy()
    atr = _atr(df, p.atr_length)
    ema = _ema(c, p.trend_ema)
    L = p.swing_length

    upper = lower = range_hi = range_lo = np.nan
    armed_long = armed_short = False    # set by a structure shift, fired on pullback
    busy_until = -1                     # bar index through which a trade is open
    trades = []

    for i in range(2 * L, n):
        # ── Confirm a swing pivot that finalizes at this bar (offset L back) ──
        piv = i - L
        win_lo, win_hi = piv - L, piv + L
        if win_lo >= 0 and win_hi < n:
            seg_h = h[win_lo:win_hi + 1]
            seg_l = lo[win_lo:win_hi + 1]
            if h[piv] == seg_h.max():
                upper = h[piv]; range_hi = h[piv]
            if lo[piv] == seg_l.min():
                lower = lo[piv]; range_lo = lo[piv]

        equilibrium = (range_hi + range_lo) / 2 if not (np.isnan(range_hi) or np.isnan(range_lo)) else np.nan

        bull_break = (not np.isnan(upper)) and c[i] > upper and c[i - 1] <= upper
        bear_break = (not np.isnan(lower)) and c[i] < lower and c[i - 1] >= lower
        if bull_break:
            armed_long, armed_short = True, False   # arm a long; wait for pullback
        if bear_break:
            armed_short, armed_long = True, False

        # While a trade is open, keep updating structure but take no new entry.
        if i <= busy_until:
            continue

        # ── Entries (flat only): a structure shift ARMS a side; we enter on the
        #     pullback into the favorable half of the range. ──
        in_disc = (not p.use_location) or np.isnan(equilibrium) or c[i] <= equilibrium
        in_prem = (not p.use_location) or np.isnan(equilibrium) or c[i] >= equilibrium
        trend_ok_long = (not p.use_trend) or c[i] > ema[i]
        trend_ok_short = (not p.use_trend) or c[i] < ema[i]

        direction = 0
        if armed_long and in_disc and trend_ok_long:
            stop = c[i] - atr[i] * p.atr_mult if p.stop_mode == "atr" else (
                (lower - atr[i] * p.struct_buf) if not np.isnan(lower) else c[i] - atr[i] * p.atr_mult)
            if c[i] - stop > 0:
                direction = 1; armed_long = False
        elif armed_short and in_prem and trend_ok_short:
            stop = c[i] + atr[i] * p.atr_mult if p.stop_mode == "atr" else (
                (upper + atr[i] * p.struct_buf) if not np.isnan(upper) else c[i] + atr[i] * p.atr_mult)
            if stop - c[i] > 0:
                direction = -1; armed_short = False

        if direction != 0:
            init_risk = abs(c[i] - stop)
            res = simulate_trade(direction, i, c[i], init_risk, h, lo, c, p)
            trades.append({
                "trade_num": len(trades) + 1,
                "type": "Entry long" if direction > 0 else "Entry short",
                "signal": "PF Long" if direction > 0 else "PF Short",
                "entry_time": pd.Timestamp(t[i]),
                "exit_time": pd.Timestamp(t[res["exit_i"]]),
                "entry_price": float(c[i]),
                "exit_price": res["exit_px"],
                "profit_usd": res["profit_usd"],
            })
            busy_until = res["exit_i"]

    cols = ["trade_num", "type", "signal", "entry_time", "exit_time",
            "entry_price", "exit_price", "profit_usd"]
    return pd.DataFrame(trades, columns=cols)
