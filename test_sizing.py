"""
Unit tests for risk-based position sizing.

Runs with pytest (`pytest test_sizing.py`) or standalone (`python
test_sizing.py`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sizing import size_position, stop_pts_from_backtest, POINT_VALUE


def test_nq_basic_floor():
    # $100k, 1% -> $1000 budget. NQ 20pt stop = $400/contract -> 2 contracts.
    s = size_position(100_000, 1.0, 20.0, "NQ")
    assert s.contracts == 2
    assert s.risk_per_contract == 400.0
    assert s.dollar_risk == 800.0
    assert s.risk_pct_actual == 0.8


def test_mnq_gives_finer_granularity():
    # Same budget, MNQ 20pt stop = $40/contract -> floor(1000/40) = 25.
    s = size_position(100_000, 1.0, 20.0, "MNQ")
    assert s.contracts == 25
    assert s.dollar_risk == 1000.0


def test_stop_too_wide_floors_to_zero():
    # $25k, 1% -> $250 budget. NQ 20pt stop = $400 > budget -> 0 contracts.
    s = size_position(25_000, 1.0, 20.0, "NQ")
    assert s.contracts == 0
    assert s.dollar_risk == 0.0


def test_realized_risk_never_exceeds_budget():
    for acct in (25_000, 50_000, 100_000, 250_000):
        for stop in (12.0, 20.0, 37.5, 60.0):
            s = size_position(acct, 1.0, stop, "NQ")
            assert s.dollar_risk <= acct * 0.01 + 1e-9


def test_point_values_are_correct():
    assert POINT_VALUE["NQ"] == 20.0 and POINT_VALUE["MNQ"] == 2.0
    assert POINT_VALUE["ES"] == 50.0 and POINT_VALUE["MES"] == 5.0


def test_custom_point_value_override():
    s = size_position(100_000, 1.0, 10.0, "FOO", point_value=100.0)
    assert s.point_value == 100.0
    assert s.contracts == 1          # 1000 budget / (10 * 100 = 1000)


def test_invalid_inputs_raise():
    for bad in (dict(account=0), dict(risk_pct=0), dict(stop_pts=0)):
        kw = dict(account=100_000, risk_pct=1.0, stop_pts=20.0)
        kw.update(bad)
        try:
            size_position(**kw)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected ValueError for {bad}")


def test_stop_pts_from_backtest_median():
    trades = pd.DataFrame({"stop_pts": [10.0, 20.0, 30.0, 40.0, 50.0]})
    assert stop_pts_from_backtest(trades) == 30.0
    assert stop_pts_from_backtest(trades, "mean") == 30.0


def test_stop_pts_from_real_backtest():
    from backtest import Params, generate_ohlc, run_backtest
    trades = run_backtest(generate_ohlc(n_bars=4000, seed=11), Params())
    sp = stop_pts_from_backtest(trades)
    assert sp > 0
    assert "stop_pts" in trades.columns


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
