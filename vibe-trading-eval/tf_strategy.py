"""Phase 3: a trend-following (momentum breakout) strategy + buy-and-hold bar.

Neither prior strategy was a trend-follower: Phantom Flow entered on the
pullback (mean-reversion entry) and Phase 2 was outright mean-reversion. Both
faded moves. On a strongly trending index the textbook edge is the opposite —
enter ON a fresh N-bar breakout, ride it with an ATR trailing stop, NO fixed
target (let winners run), aligned with the slow trend, gated to normal vol.

Because NAS100 tripled over 2016-2020, any long-biased system shows profit from
beta alone. So `buy_and_hold_metrics` computes the benchmark this must beat on
a risk-adjusted basis — profit that doesn't beat buy-and-hold's return-per-unit
-drawdown is not alpha.

Emits the analyzer trade schema so the gauntlet plugs in unchanged.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import _atr, _ema, load_ohlc_csv


@dataclass
class TFParams:
    channel_len: int = 40       # Donchian breakout lookback
    atr_len: int = 14
    trail_atr: float = 3.0      # ATR-multiple trailing stop (the only exit)
    trend_len: int = 200        # slow EMA; only trade in its direction
    allow_short: bool = True
    use_vol_filter: bool = True
    vol_lookback: int = 200
    vol_lo_pct: float = 0.20
    vol_hi_pct: float = 0.80
    size_mode: str = "risk"
    risk_dollars: float = 500.0
    contracts: int = 2
    max_contracts: int = 20
    multiplier: float = 20.0
    commission: float = 2.0


def buy_and_hold_metrics(ohlc: pd.DataFrame, bars_per_year: int) -> dict:
    """Return-per-drawdown benchmark: hold the index long the whole window."""
    c = ohlc["close"].to_numpy(dtype=float)
    total_ret = (c[-1] / c[0] - 1.0) * 100
    peak = np.maximum.accumulate(c)
    max_dd = float(((c - peak) / peak * 100).min())
    rets = np.diff(c) / c[:-1]
    sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 0 else 0.0
    return {"total_return_pct": round(total_ret, 1), "max_dd_pct": round(max_dd, 1),
            "sharpe": round(sharpe, 2), "ret_over_dd": round(abs(total_ret / max_dd), 2) if max_dd else 0.0}


def run_tf(ohlc: pd.DataFrame, p: TFParams = TFParams()) -> pd.DataFrame:
    df = ohlc.reset_index(drop=True)
    n = len(df)
    h, lo, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    t = df["time"].to_numpy()
    atr = _atr(df, p.atr_len)
    trend = _ema(c, p.trend_len)

    # Donchian channels on PRIOR bars (shifted by 1) — no lookahead.
    hi_ser = pd.Series(h).rolling(p.channel_len).max().shift(1).to_numpy()
    lo_ser = pd.Series(lo).rolling(p.channel_len).min().shift(1).to_numpy()

    vol_rank = np.full(n, 0.5)
    if p.use_vol_filter:
        for i in range(1, n):
            w = atr[max(0, i - p.vol_lookback):i]
            vol_rank[i] = float(np.mean(w <= atr[i])) if len(w) else 0.5

    trades = []
    i = max(p.channel_len, p.trend_len, p.atr_len) + 1
    while i < n - 1:
        vol_ok = (not p.use_vol_filter) or (p.vol_lo_pct <= vol_rank[i] <= p.vol_hi_pct)
        if not vol_ok or np.isnan(hi_ser[i]) or atr[i] <= 0:
            i += 1
            continue

        direction = 0
        if c[i] > hi_ser[i] and c[i] > trend[i]:
            direction = 1
        elif p.allow_short and c[i] < lo_ser[i] and c[i] < trend[i]:
            direction = -1
        if direction == 0:
            i += 1
            continue

        entry_px = c[i]
        stop = entry_px - direction * p.trail_atr * atr[i]
        init_risk = abs(entry_px - stop)
        if init_risk <= 0:
            i += 1
            continue
        if p.size_mode == "risk":
            per_ct = init_risk * p.multiplier
            nc = max(1, min(p.max_contracts, round(p.risk_dollars / per_ct))) if per_ct > 0 else 1
        else:
            nc = p.contracts

        # Ride with a trailing stop; no profit target (let winners run).
        exit_i, exit_px = n - 1, c[n - 1]
        hwm = entry_px if direction > 0 else entry_px
        for j in range(i + 1, n):
            if direction > 0:
                hwm = max(hwm, h[j])
                stop = max(stop, hwm - p.trail_atr * atr[j])
                if lo[j] <= stop:
                    exit_i, exit_px = j, stop
                    break
            else:
                hwm = min(hwm, lo[j])
                stop = min(stop, hwm + p.trail_atr * atr[j])
                if h[j] >= stop:
                    exit_i, exit_px = j, stop
                    break
            exit_i, exit_px = j, c[j]

        gross = (exit_px - entry_px) * direction * nc * p.multiplier
        profit = gross - 2 * nc * p.commission
        # R-multiple: P&L in units of initial risk, instrument-agnostic. Lets a
        # multi-instrument portfolio combine trades with different price scales
        # and contract specs on a common risk basis.
        r_multiple = (exit_px - entry_px) * direction / init_risk
        trades.append({
            "trade_num": len(trades) + 1,
            "type": "Entry long" if direction > 0 else "Entry short",
            "signal": "TF Long" if direction > 0 else "TF Short",
            "entry_time": pd.Timestamp(t[i]),
            "exit_time": pd.Timestamp(t[exit_i]),
            "entry_price": float(entry_px),
            "exit_price": float(exit_px),
            "profit_usd": round(float(profit), 2),
            "r_multiple": round(float(r_multiple), 4),
        })
        i = exit_i + 1

    cols = ["trade_num", "type", "signal", "entry_time", "exit_time",
            "entry_price", "exit_price", "profit_usd", "r_multiple"]
    return pd.DataFrame(trades, columns=cols)


if __name__ == "__main__":
    from metrics import calc_metrics
    CAP = 50_000.0
    for name, path, bpy in [("1H full", "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv", 24 * 252),
                            ("15m calm", "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv", 96 * 252),
                            ("15m vol", "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv", 96 * 252)]:
        ohlc = load_ohlc_csv(path)
        bh = buy_and_hold_metrics(ohlc, bpy)
        print(f"\n=== {name} ===")
        print(f"  BUY & HOLD:  return {bh['total_return_pct']}%  maxDD {bh['max_dd_pct']}%  "
              f"Sharpe {bh['sharpe']}  ret/DD {bh['ret_over_dd']}")
        for short in (True, False):
            tr = run_tf(ohlc, TFParams(allow_short=short))
            if len(tr) < 5:
                print(f"  TF short={short}: {len(tr)} trades (too few)")
                continue
            m = calc_metrics(tr, CAP)
            print(f"  TF short={str(short):<5} {m['n_trades']:>4} tr  net ${m['net_profit']:>9,.0f}  "
                  f"PF {m['profit_factor']:.2f}  WR {m['win_rate']:.0f}%  DD {m['max_dd']:.1f}%  "
                  f"ret {m['return_pct']:.1f}%")
