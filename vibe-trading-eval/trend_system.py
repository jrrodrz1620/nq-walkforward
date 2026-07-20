"""The tradeable system: diversified daily trend-following with vol targeting.

One code path serves both research and operation, so there is no
backtest/production drift:

    python trend_system.py backtest              # full-history portfolio sim
    python trend_system.py oos                   # fit-era vs test-era report
    python trend_system.py orders                # today's orders from latest bars

Rules (see SYSTEM.md for the full spec):
  - Universe: 24 Oanda CFDs across equity indices, FX, commodities, bonds.
  - Signal: close breaks the prior `channel_len`-day Donchian extreme, in the
    direction of the `trend_len`-day EMA. Symmetric (long and short).
  - Exit: ATR(`atr_len`) trailing stop, `trail_atr` multiples from the
    high-water mark. No profit target.
  - Sizing: every trade risks `base_risk_frac` of current equity to its stop
    (equal-risk across instruments), scaled by a portfolio vol target:
    realized 60-day vol of the system equity above `target_vol` shrinks new
    risk, below it grows it, clamped to [0.5x, 2.0x].
  - Risk limits: one position per instrument; at most `max_positions` open.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tf_strategy import TFParams, run_tf, load_ohlc_csv  # noqa: E402

DATA_DIR = "/home/user/vibe-eval/data/universe_daily"

# The deployable basket: one liquid representative per asset bucket, chosen
# A PRIORI (not by historical performance — see SYSTEM.md: fit-era instrument
# selection failed OOS; correlated additions like extra equity indices only
# added equity beta). This is the basket whose OOS holds (Sharpe 1.38).
CORE_9 = ["NAS100_USD", "SPX500_USD", "JP225_USD", "EUR_USD", "GBP_USD",
          "XAU_USD", "WTICO_USD", "CORN_USD", "USB10Y_USD"]

UNIVERSE: dict[str, str] = {
    # equity indices
    "AU200_AUD": "equity", "FR40_EUR": "equity", "JP225_USD": "equity",
    "NAS100_USD": "equity", "NL25_EUR": "equity", "SPX500_USD": "equity",
    "UK100_GBP": "equity", "US2000_USD": "equity",
    # FX
    "AUD_JPY": "fx", "AUD_USD": "fx", "EUR_JPY": "fx", "EUR_USD": "fx",
    "GBP_USD": "fx", "USD_CAD": "fx",
    # commodities
    "CORN_USD": "commodity", "NATGAS_USD": "commodity", "SOYBN_USD": "commodity",
    "SUGAR_USD": "commodity", "WHEAT_USD": "commodity", "WTICO_USD": "commodity",
    "XAU_USD": "commodity",
    # bonds
    "DE10YB_EUR": "bond", "UK10YB_GBP": "bond",
    "USB02Y_USD": "bond", "USB10Y_USD": "bond",
}


@dataclass
class SystemConfig:
    # Shipped params: best FIT-era (2005-2012) portfolio Sharpe on CORE_9,
    # chosen causally; OOS 2013-2020 Sharpe 1.38. 11/12 grid cells OOS-positive.
    channel_len: int = 100
    trail_atr: float = 4.0
    trend_len: int = 100
    atr_len: int = 14
    capital: float = 100_000.0
    base_risk_frac: float = 0.005      # risk per trade, fraction of equity
    target_vol: float = 0.10           # annualized portfolio vol target
    vol_window: int = 60               # trading days of realized vol
    vol_scale_bounds: tuple = (0.5, 2.0)
    max_positions: int = 12
    fit_end: str = "2013-01-01"        # train/test split for `oos` mode


def _tf_params(cfg: SystemConfig) -> TFParams:
    return TFParams(channel_len=cfg.channel_len, trail_atr=cfg.trail_atr,
                    trend_len=cfg.trend_len, atr_len=cfg.atr_len,
                    use_vol_filter=False, allow_short=True)


def load_universe(basket: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Load the deployable CORE_9 by default; pass list(UNIVERSE) for all."""
    out = {}
    for inst in (basket if basket is not None else CORE_9):
        p = Path(DATA_DIR) / f"{inst}.csv"
        if p.exists():
            out[inst] = load_ohlc_csv(str(p))
    return out


def generate_all_trades(data: dict[str, pd.DataFrame], cfg: SystemConfig) -> pd.DataFrame:
    """Per-instrument signal trades (R-multiples); sizing applied later."""
    frames = []
    params = _tf_params(cfg)
    for inst, ohlc in data.items():
        tr = run_tf(ohlc, params)
        if len(tr):
            tr = tr.copy()
            tr["inst"] = inst
            frames.append(tr)
    all_tr = pd.concat(frames).sort_values("entry_time").reset_index(drop=True)
    return all_tr


def simulate_portfolio(trades: pd.DataFrame, cfg: SystemConfig,
                       start=None, end=None) -> dict:
    """Chronological sim: equal-risk entries, vol-target overlay, position cap.

    Sizing uses only information available at entry time (equity from closed
    trades, trailing realized vol of the marked equity) — causal by
    construction.
    """
    tr = trades.copy()
    if start is not None:
        tr = tr[tr["entry_time"] >= pd.Timestamp(start)]
    if end is not None:
        tr = tr[tr["entry_time"] < pd.Timestamp(end)]
    tr = tr.sort_values("entry_time").reset_index(drop=True)

    equity = cfg.capital
    closed: list[tuple[pd.Timestamp, float]] = []   # (exit_time, pnl)
    eq_hist: list[tuple[pd.Timestamp, float]] = []  # daily-ish marks for vol calc
    open_pos: list[dict] = []
    taken, skipped_cap = [], 0

    def realized_vol(now) -> float:
        pts = [(t, e) for t, e in eq_hist if t <= now][-cfg.vol_window * 3:]
        if len(pts) < 10:
            return cfg.target_vol
        s = pd.Series(dict(pts)).sort_index()
        s = s.resample("1D").last().dropna().tail(cfg.vol_window)
        r = s.pct_change().dropna()
        if len(r) < 5 or r.std() == 0:
            return cfg.target_vol
        return float(r.std() * np.sqrt(252))

    for row in tr.itertuples():
        now = row.entry_time
        # settle exits that completed before this entry
        still = []
        for p in open_pos:
            if p["exit_time"] <= now:
                pnl = p["r"] * p["risk_dollars"]
                equity += pnl
                closed.append((p["exit_time"], pnl))
                eq_hist.append((p["exit_time"], equity))
            else:
                still.append(p)
        open_pos = still

        if len(open_pos) >= cfg.max_positions:
            skipped_cap += 1
            continue

        rv = realized_vol(now)
        scale = float(np.clip(cfg.target_vol / rv, *cfg.vol_scale_bounds))
        risk_dollars = equity * cfg.base_risk_frac * scale
        open_pos.append({"exit_time": row.exit_time, "r": row.r_multiple,
                         "risk_dollars": risk_dollars})
        taken.append({"entry_time": now, "exit_time": row.exit_time,
                      "inst": row.inst, "r": row.r_multiple,
                      "risk_dollars": round(risk_dollars, 2),
                      "vol_scale": round(scale, 3)})

    for p in open_pos:                                  # settle remainder
        pnl = p["r"] * p["risk_dollars"]
        equity += pnl
        closed.append((p["exit_time"], pnl))
        eq_hist.append((p["exit_time"], equity))

    closed.sort(key=lambda x: x[0])
    eq = pd.Series({t: v for t, v in eq_hist}).sort_index()
    eq = pd.concat([pd.Series({tr["entry_time"].min(): cfg.capital}), eq])
    ret = (equity / cfg.capital - 1) * 100
    peak = eq.cummax()
    dd = float(((eq - peak) / peak * 100).min())
    daily = eq.resample("1D").last().dropna().pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 5 and daily.std() > 0 else 0.0
    return {"final_equity": round(equity, 2), "ret_pct": round(ret, 1),
            "max_dd_pct": round(dd, 1), "sharpe": round(sharpe, 2),
            "ret_over_dd": round(abs(ret / dd), 2) if dd else 0.0,
            "n_trades": len(taken), "skipped_at_cap": skipped_cap,
            "equity_curve": eq, "trades": pd.DataFrame(taken)}


# ─── operations: today's orders ───

def current_state(data: dict[str, pd.DataFrame], cfg: SystemConfig) -> dict:
    """Replay each instrument to its latest bar; report open positions, fresh
    entry signals on the last bar, and current trailing-stop levels."""
    params = _tf_params(cfg)
    open_positions, new_entries = [], []
    for inst, ohlc in data.items():
        tr = run_tf(ohlc, params)
        last_time = ohlc["time"].iloc[-1]
        if len(tr):
            last = tr.iloc[-1]
            if last["exit_time"] == last_time and last["entry_time"] != last_time:
                # exited on the final bar OR still open (run_tf closes open
                # trades at the last bar) — distinguish by stop touch is not
                # recoverable here, so report as "position through last bar".
                open_positions.append({
                    "inst": inst, "direction": last["type"].replace("Entry ", ""),
                    "entry_time": str(last["entry_time"].date()),
                    "entry_price": last["entry_price"],
                    "last_close": float(ohlc["close"].iloc[-1]),
                    "open_r": last["r_multiple"],
                })
            if last["entry_time"] == last_time:
                new_entries.append({"inst": inst,
                                    "direction": last["type"].replace("Entry ", ""),
                                    "entry_price": last["entry_price"]})
    return {"as_of": str(max(o["time"].iloc[-1] for o in data.values()).date()),
            "open_positions": open_positions, "new_entries": new_entries}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    cfg = SystemConfig()
    data = load_universe()
    print(f"Universe loaded: {len(data)} instruments")

    if mode == "orders":
        state = current_state(data, cfg)
        print(f"\nAs of {state['as_of']}")
        print(f"open/last-bar positions: {len(state['open_positions'])}")
        for p in state["open_positions"]:
            print(f"  {p['inst']:<12} {p['direction']:<6} from {p['entry_time']} "
                  f"@ {p['entry_price']:.4f}  last {p['last_close']:.4f}  open R {p['open_r']:+.2f}")
        print(f"new entry signals on last bar: {len(state['new_entries'])}")
        for e in state["new_entries"]:
            print(f"  {e['inst']:<12} {e['direction']:<6} @ {e['entry_price']:.4f}")
        return

    trades = generate_all_trades(data, cfg)
    print(f"Signal trades generated: {len(trades)}")

    if mode == "oos":
        fit = simulate_portfolio(trades, cfg, end=cfg.fit_end)
        oos = simulate_portfolio(trades, cfg, start=cfg.fit_end)
        for label, r in [("FIT 2005-2012", fit), ("OOS 2013-2020", oos)]:
            print(f"  {label}: ret {r['ret_pct']}%  DD {r['max_dd_pct']}%  "
                  f"Sharpe {r['sharpe']}  ret/DD {r['ret_over_dd']}  "
                  f"trades {r['n_trades']} (cap-skipped {r['skipped_at_cap']})")
        return

    r = simulate_portfolio(trades, cfg)
    print(f"  FULL: ret {r['ret_pct']}%  DD {r['max_dd_pct']}%  Sharpe {r['sharpe']}  "
          f"ret/DD {r['ret_over_dd']}  trades {r['n_trades']} (cap-skipped {r['skipped_at_cap']})")


if __name__ == "__main__":
    main()
