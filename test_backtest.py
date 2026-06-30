"""
Unit tests for the Phantom Flow SMC backtest fill logic.

Runs with pytest (`pytest test_backtest.py`) or standalone (`python
test_backtest.py`). The pure `simulate_trade` function is tested with crafted
bars so each fill rule is checked in isolation; `run_backtest` is checked for
structural invariants.
"""
from __future__ import annotations

import dataclasses

import numpy as np

from backtest import Params, simulate_trade, run_backtest, generate_ohlc


def _p(**kw) -> Params:
    """Params with deterministic, simple economics for arithmetic checks."""
    base = dict(contracts=2, multiplier=20.0, commission=2.0, rr_ratio=2.0,
                use_be=False, use_partial=False, partial_pct=50.0, be_trigger=1.0)
    base.update(kw)
    return Params(**base)


# entry comm = contracts*comm = 4 ; exit comm = open_contracts*comm
def test_long_hits_target():
    # entry 100, risk 10 -> tp 120, stop 90. Bar reaches 120, never 90.
    h = np.array([100.0, 120.0]); lo = np.array([100.0, 95.0]); c = np.array([100.0, 118.0])
    r = simulate_trade(1, 0, 100.0, 10.0, h, lo, c, _p())
    # gross = 20 * 2 * 20 = 800 ; -4 entry -4 exit = 792
    assert r["exit_px"] == 120.0
    assert r["profit_usd"] == 792.0


def test_long_hits_stop():
    h = np.array([100.0, 105.0]); lo = np.array([100.0, 90.0]); c = np.array([100.0, 95.0])
    r = simulate_trade(1, 0, 100.0, 10.0, h, lo, c, _p())
    # gross = -10 * 2 * 20 = -400 ; -8 comm = -408
    assert r["exit_px"] == 90.0
    assert r["profit_usd"] == -408.0


def test_stop_fills_before_target_when_both_touched():
    # Bar touches BOTH 90 (stop) and 120 (target). Conservative rule -> stop.
    h = np.array([100.0, 120.0]); lo = np.array([100.0, 90.0]); c = np.array([100.0, 110.0])
    r = simulate_trade(1, 0, 100.0, 10.0, h, lo, c, _p())
    assert r["exit_px"] == 90.0
    assert r["profit_usd"] == -408.0


def test_partial_then_final_target():
    # Bar1 hits tp1=110 (partial + BE), bar2 hits tp_final=120.
    h = np.array([100.0, 110.0, 120.0]); lo = np.array([100.0, 105.0, 101.0])
    c = np.array([100.0, 108.0, 118.0])
    r = simulate_trade(1, 0, 100.0, 10.0, h, lo, c, _p(use_partial=True, use_be=True))
    # partial leg: (110-100)*1*20 - 1*2 = 198 ; entry comm -4 -> realized 194
    # final leg:   (120-100)*1*20 = 400 ; -1*2 exit comm -> 592 total
    assert r["exit_px"] == 120.0
    assert r["profit_usd"] == 592.0


def test_short_hits_target():
    # short entry 100, risk 10 -> tp 80, stop 110. Bar reaches 80, never 110.
    h = np.array([100.0, 105.0]); lo = np.array([100.0, 80.0]); c = np.array([100.0, 85.0])
    r = simulate_trade(-1, 0, 100.0, 10.0, h, lo, c, _p())
    assert r["exit_px"] == 80.0
    assert r["profit_usd"] == 792.0


def test_runs_to_end_closes_at_last_close():
    # Neither stop nor target hit; close at final bar close (102).
    h = np.array([100.0, 105.0, 105.0]); lo = np.array([100.0, 95.0, 95.0])
    c = np.array([100.0, 101.0, 102.0])
    r = simulate_trade(1, 0, 100.0, 10.0, h, lo, c, _p())
    # gross = 2 * 2 * 20 = 80 ; -8 comm = 72
    assert r["exit_i"] == 2
    assert r["profit_usd"] == 72.0


def test_no_partial_keeps_full_size_to_target():
    # With partial OFF, full 2 contracts close at target.
    h = np.array([100.0, 120.0]); lo = np.array([100.0, 99.0]); c = np.array([100.0, 118.0])
    r = simulate_trade(1, 0, 100.0, 10.0, h, lo, c, _p(use_partial=False))
    assert r["profit_usd"] == 792.0


# ── Integration invariants on run_backtest ──

def test_flat_data_makes_no_trades():
    import pandas as pd
    n = 500
    px = np.full(n, 100.0)
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="15min"),
        "open": px, "high": px, "low": px, "close": px,
    })
    trades = run_backtest(df, _p())
    assert len(trades) == 0


def test_synthetic_run_is_deterministic_and_nonoverlapping():
    ohlc = generate_ohlc(n_bars=4000, seed=11)
    trades = run_backtest(ohlc, Params())
    assert len(trades) == 42                       # locks behavior; update if logic changes
    # Trades must not overlap: each exit <= next entry.
    ex = trades["exit_time"].to_numpy()
    en = trades["entry_time"].to_numpy()
    assert (en[1:] >= ex[:-1]).all()
    # Every trade exits at or after it enters.
    assert (trades["exit_time"] >= trades["entry_time"]).all()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}  {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {fn.__name__}  {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} tests passed")
    raise SystemExit(0 if passed == len(fns) else 1)
