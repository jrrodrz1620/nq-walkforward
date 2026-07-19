"""Phantom Flow SMC ported to the Vibe-Trading signal-engine contract.

Faithful port of nq-walkforward/backtest.py: confirmed swing pivots ->
BOS arms a side -> entry on pullback into discount/premium with EMA trend
filter -> structure/ATR stop -> breakeven at 1R -> 2R target.

Differences forced by the platform contract (bar-close weights, next-bar-open
fills):
  - entries/exits execute at the NEXT bar open, not at the signal bar close
    or the exact stop/target price;
  - no partial exits (the engine cannot resize an open position), so the
    partial leg is disabled — compare against Pipeline A2.

Signal semantics: +1 long, -1 short, 0 flat; the engine shifts by one bar.
"""

from typing import Dict

import numpy as np
import pandas as pd

SWING_LENGTH = 10
ATR_LENGTH = 14
STOP_MODE = "structure"  # "structure" | "atr"
ATR_MULT = 1.5
STRUCT_BUF = 0.25
RR_RATIO = 2.0
USE_LOCATION = True
USE_TREND = True
TREND_EMA = 50
USE_BE = True
BE_TRIGGER = 1.0


def _atr(h: np.ndarray, lo: np.ndarray, c: np.ndarray, length: int) -> np.ndarray:
    prev_c = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - lo, np.maximum(np.abs(h - prev_c), np.abs(lo - prev_c)))
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


class SignalEngine:
    def generate(self, data_map: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        out: Dict[str, pd.Series] = {}
        for code, df in data_map.items():
            out[code] = self._one(df)
        return out

    def _one(self, df: pd.DataFrame) -> pd.Series:
        h = df["high"].to_numpy(dtype=float)
        lo = df["low"].to_numpy(dtype=float)
        c = df["close"].to_numpy(dtype=float)
        n = len(c)
        sig = np.zeros(n)
        if n < 2 * SWING_LENGTH + 2:
            return pd.Series(sig, index=df.index)

        atr = _atr(h, lo, c, ATR_LENGTH)
        ema = _ema(c, TREND_EMA)
        L = SWING_LENGTH

        upper = lower = range_hi = range_lo = np.nan
        armed_long = armed_short = False
        pos_dir = 0
        entry_ref = work_stop = tp_final = init_risk = np.nan

        for i in range(2 * L, n):
            # confirm swing pivot finalized at this bar (offset L back)
            piv = i - L
            win_lo = piv - L
            if win_lo >= 0:
                seg_h = h[win_lo:piv + L + 1]
                seg_l = lo[win_lo:piv + L + 1]
                if h[piv] == seg_h.max():
                    upper = h[piv]
                    range_hi = h[piv]
                if lo[piv] == seg_l.min():
                    lower = lo[piv]
                    range_lo = lo[piv]

            equilibrium = (
                (range_hi + range_lo) / 2
                if not (np.isnan(range_hi) or np.isnan(range_lo))
                else np.nan
            )

            if not np.isnan(upper) and c[i] > upper and c[i - 1] <= upper:
                armed_long, armed_short = True, False
            if not np.isnan(lower) and c[i] < lower and c[i - 1] >= lower:
                armed_short, armed_long = True, False

            # manage open position on this bar's extremes
            if pos_dir != 0:
                exited = False
                if pos_dir > 0:
                    if USE_BE and h[i] >= entry_ref + init_risk * BE_TRIGGER:
                        work_stop = max(work_stop, entry_ref)
                    if lo[i] <= work_stop or h[i] >= tp_final:
                        exited = True
                else:
                    if USE_BE and lo[i] <= entry_ref - init_risk * BE_TRIGGER:
                        work_stop = min(work_stop, entry_ref)
                    if h[i] >= work_stop or lo[i] <= tp_final:
                        exited = True
                if exited:
                    pos_dir = 0
                    sig[i] = 0.0
                else:
                    sig[i] = float(pos_dir)
                continue

            # flat: check entries
            in_disc = (not USE_LOCATION) or np.isnan(equilibrium) or c[i] <= equilibrium
            in_prem = (not USE_LOCATION) or np.isnan(equilibrium) or c[i] >= equilibrium
            trend_ok_long = (not USE_TREND) or c[i] > ema[i]
            trend_ok_short = (not USE_TREND) or c[i] < ema[i]

            direction = 0
            stop = np.nan
            if armed_long and in_disc and trend_ok_long:
                if STOP_MODE == "atr" or np.isnan(lower):
                    stop = c[i] - atr[i] * ATR_MULT
                else:
                    stop = lower - atr[i] * STRUCT_BUF
                if c[i] - stop > 0:
                    direction = 1
                    armed_long = False
            elif armed_short and in_prem and trend_ok_short:
                if STOP_MODE == "atr" or np.isnan(upper):
                    stop = c[i] + atr[i] * ATR_MULT
                else:
                    stop = upper + atr[i] * STRUCT_BUF
                if stop - c[i] > 0:
                    direction = -1
                    armed_short = False

            if direction != 0:
                pos_dir = direction
                entry_ref = c[i]
                init_risk = abs(entry_ref - stop)
                work_stop = stop
                tp_final = entry_ref + direction * init_risk * RR_RATIO
                sig[i] = float(direction)

        return pd.Series(sig, index=df.index)
