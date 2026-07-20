"""Unit tests for trend_system's portfolio simulation and sizing.

Run: python test_trend_system.py (from vibe-trading-eval/, needs repo root on
path) or via the repo's vibe-env python.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trend_system import SystemConfig, simulate_portfolio  # noqa: E402


def _trades(rows):
    return pd.DataFrame(rows, columns=["entry_time", "exit_time", "inst", "r_multiple"])


def _t(s):
    return pd.Timestamp(s)


def test_single_trade_pnl_is_risk_times_r():
    cfg = SystemConfig(capital=100_000, base_risk_frac=0.01,
                       vol_scale_bounds=(1.0, 1.0))  # overlay off
    tr = _trades([(_t("2015-01-05"), _t("2015-02-01"), "A", 2.0)])
    r = simulate_portfolio(tr, cfg)
    # one trade risking 1% of 100k at +2R -> +2000
    assert r["final_equity"] == 102_000.0
    assert r["n_trades"] == 1


def test_equity_compounds_between_trades():
    cfg = SystemConfig(capital=100_000, base_risk_frac=0.01,
                       vol_scale_bounds=(1.0, 1.0))
    tr = _trades([
        (_t("2015-01-05"), _t("2015-02-01"), "A", 1.0),   # +1000 -> 101,000
        (_t("2015-03-01"), _t("2015-04-01"), "A", -1.0),  # risks 1% of 101,000
    ])
    r = simulate_portfolio(tr, cfg)
    assert r["final_equity"] == 101_000.0 - 1_010.0


def test_position_cap_skips_excess_concurrent_entries():
    cfg = SystemConfig(capital=100_000, max_positions=2,
                       vol_scale_bounds=(1.0, 1.0))
    # three positions all open simultaneously -> third must be skipped
    tr = _trades([
        (_t("2015-01-05"), _t("2015-06-01"), "A", 1.0),
        (_t("2015-01-06"), _t("2015-06-01"), "B", 1.0),
        (_t("2015-01-07"), _t("2015-06-01"), "C", 1.0),
    ])
    r = simulate_portfolio(tr, cfg)
    assert r["n_trades"] == 2
    assert r["skipped_at_cap"] == 1


def test_vol_scale_stays_within_bounds():
    cfg = SystemConfig(capital=100_000, vol_scale_bounds=(0.5, 2.0))
    rng = np.random.default_rng(3)
    rows, day = [], pd.Timestamp("2014-01-06")
    for k in range(120):
        rows.append((day, day + pd.Timedelta(days=3), "A", float(rng.normal(0, 1))))
        day += pd.Timedelta(days=4)
    r = simulate_portfolio(_trades(rows), cfg)
    scales = r["trades"]["vol_scale"]
    assert scales.min() >= 0.5 - 1e-9
    assert scales.max() <= 2.0 + 1e-9
    # overlay must actually vary once history exists
    assert scales.nunique() > 1


def test_date_window_filters_entries():
    cfg = SystemConfig(vol_scale_bounds=(1.0, 1.0))
    tr = _trades([
        (_t("2012-06-01"), _t("2012-07-01"), "A", 1.0),
        (_t("2014-06-01"), _t("2014-07-01"), "A", 1.0),
    ])
    fit = simulate_portfolio(tr, cfg, end="2013-01-01")
    oos = simulate_portfolio(tr, cfg, start="2013-01-01")
    assert fit["n_trades"] == 1 and oos["n_trades"] == 1


def test_drawdown_is_negative_when_losses_occur():
    cfg = SystemConfig(vol_scale_bounds=(1.0, 1.0))
    tr = _trades([
        (_t("2015-01-05"), _t("2015-02-01"), "A", -1.0),
        (_t("2015-03-01"), _t("2015-04-01"), "A", -1.0),
        (_t("2015-05-01"), _t("2015-06-01"), "A", 3.0),
    ])
    r = simulate_portfolio(tr, cfg)
    assert r["max_dd_pct"] < 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
