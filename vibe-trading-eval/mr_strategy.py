"""Phase 2: a fresh mean-reversion strategy on the normal-vol-regime primitive.

Rationale (from SQUEEZE_RESULTS.md): Phantom Flow's ceiling was thin because
its trend-breakout entry had a 43-45% win rate — barely over break-even for 2R
geometry. Mean-reversion attacks that directly: fade a stretch back to the
mean, which is high-win-rate by construction (small target, occasional larger
stop). Gated to the same robust mid-volatility band, risk-sized.

Emits the analyzer's trade schema, so metrics / significance / WFO plug in
unchanged. Same conservative intrabar rule as the repo: if a bar touches both
the stop and the target, the stop fills first.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import _atr, _ema, load_ohlc_csv  # reuse indicators


@dataclass
class MRParams:
    mean_len: int = 20          # EMA that price reverts to
    atr_len: int = 14
    entry_z: float = 2.0        # enter when |close - mean| >= entry_z * ATR
    stop_atr: float = 1.5       # stop this many ATR beyond entry (past the stretch)
    trend_len: int = 200        # slow EMA regime filter
    trend_mode: str = "with"    # "with" = only fade toward-trend side | "off"
    max_hold: int = 40          # force exit after this many bars
    # normal-vol gate (same primitive that survived OOS on Phantom Flow)
    use_vol_filter: bool = True
    vol_lookback: int = 200
    vol_lo_pct: float = 0.20
    vol_hi_pct: float = 0.80
    # sizing
    size_mode: str = "risk"     # "risk" | "fixed"
    risk_dollars: float = 500.0
    contracts: int = 2
    max_contracts: int = 20
    multiplier: float = 20.0
    commission: float = 2.0


def run_mr(ohlc: pd.DataFrame, p: MRParams = MRParams()) -> pd.DataFrame:
    df = ohlc.reset_index(drop=True)
    n = len(df)
    h, lo, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    t = df["time"].to_numpy()
    atr = _atr(df, p.atr_len)
    mean = _ema(c, p.mean_len)
    trend = _ema(c, p.trend_len)

    vol_rank = np.full(n, 0.5)
    if p.use_vol_filter:
        for i in range(1, n):
            w = atr[max(0, i - p.vol_lookback):i]
            vol_rank[i] = float(np.mean(w <= atr[i])) if len(w) else 0.5

    trades = []
    i = max(p.mean_len, p.atr_len, p.trend_len)
    while i < n - 1:
        if atr[i] <= 0:
            i += 1
            continue
        z = (c[i] - mean[i]) / atr[i]
        vol_ok = (not p.use_vol_filter) or (p.vol_lo_pct <= vol_rank[i] <= p.vol_hi_pct)
        if not vol_ok:
            i += 1
            continue

        # Fade the stretch. trend_mode "with": only take the fade that ends up
        # trading in the direction of the slow trend (long dips in an uptrend,
        # short rips in a downtrend).
        direction = 0
        if z <= -p.entry_z and (p.trend_mode == "off" or c[i] > trend[i]):
            direction = 1
        elif z >= p.entry_z and (p.trend_mode == "off" or c[i] < trend[i]):
            direction = -1
        if direction == 0:
            i += 1
            continue

        entry_px = c[i]
        stop = entry_px - direction * p.stop_atr * atr[i]
        init_risk = abs(entry_px - stop)
        if init_risk <= 0:
            i += 1
            continue
        if p.size_mode == "risk":
            per_ct = init_risk * p.multiplier
            nc = max(1, min(p.max_contracts, round(p.risk_dollars / per_ct))) if per_ct > 0 else 1
        else:
            nc = p.contracts

        # Walk forward: target is the mean (dynamic), stop is fixed, max_hold caps it.
        exit_i, exit_px = n - 1, c[n - 1]
        for j in range(i + 1, min(i + 1 + p.max_hold, n)):
            if direction > 0:
                if lo[j] <= stop:              # stop first (conservative)
                    exit_i, exit_px = j, stop
                    break
                if h[j] >= mean[j]:            # reverted to mean = target
                    exit_i, exit_px = j, mean[j]
                    break
            else:
                if h[j] >= stop:
                    exit_i, exit_px = j, stop
                    break
                if lo[j] <= mean[j]:
                    exit_i, exit_px = j, mean[j]
                    break
            exit_i, exit_px = j, c[j]

        gross = (exit_px - entry_px) * direction * nc * p.multiplier
        profit = gross - 2 * nc * p.commission
        trades.append({
            "trade_num": len(trades) + 1,
            "type": "Entry long" if direction > 0 else "Entry short",
            "signal": "MR Long" if direction > 0 else "MR Short",
            "entry_time": pd.Timestamp(t[i]),
            "exit_time": pd.Timestamp(t[exit_i]),
            "entry_price": float(entry_px),
            "exit_price": float(exit_px),
            "profit_usd": round(float(profit), 2),
        })
        i = exit_i + 1        # flat-only: no new entry until the trade closes

    cols = ["trade_num", "type", "signal", "entry_time", "exit_time",
            "entry_price", "exit_price", "profit_usd"]
    return pd.DataFrame(trades, columns=cols)


if __name__ == "__main__":
    from metrics import calc_metrics
    CAP = 50_000.0
    for name, path in [("1H full", "/home/user/vibe-eval/data/nas100_1h_2016_2020.csv"),
                       ("15m calm", "/home/user/vibe-eval/data/nas100_15m_2016_2018.csv"),
                       ("15m vol", "/home/user/vibe-eval/data/nas100_15m_2019_2020.csv")]:
        ohlc = load_ohlc_csv(path)
        print(f"\n=== {name} — mean-reversion (defaults) ===")
        for tm in ("with", "off"):
            tr = run_mr(ohlc, MRParams(trend_mode=tm))
            if len(tr) < 5:
                print(f"  trend={tm:<4} {len(tr)} trades (too few)")
                continue
            m = calc_metrics(tr, CAP)
            print(f"  trend={tm:<4} {m['n_trades']:>4} tr  net ${m['net_profit']:>8,.0f}  "
                  f"PF {m['profit_factor']:.2f}  WR {m['win_rate']:.0f}%  DD {m['max_dd']:.1f}%")
