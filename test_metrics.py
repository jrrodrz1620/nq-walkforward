"""
Unit tests for the significance helpers in metrics.py.

Runs with pytest (`pytest test_metrics.py`) or standalone (`python
test_metrics.py`). The permutation test is checked against sequences whose
path quality is knowable by construction; the bootstrap CI against the exact
Sharpe of crafted trade lists.
"""
from __future__ import annotations

import numpy as np

from metrics import permutation_test, bootstrap_sharpe_ci, _path_stats

CAPITAL = 50_000.0


# ── _path_stats ──

def test_path_stats_monotonic_up_has_no_drawdown():
    sharpe, maxdd = _path_stats(np.full(20, 100.0), CAPITAL)
    assert maxdd == 0.0
    # constant positive steps on a growing base: tiny negative return drift
    # in pct terms is impossible — Sharpe must be large and positive
    assert sharpe > 1.0


def test_path_stats_drawdown_sign():
    # up then down: equity peaks mid-path, ends below peak
    pnl = np.array([1000.0, 1000.0, -1500.0, -500.0])
    _, maxdd = _path_stats(pnl, CAPITAL)
    assert maxdd < 0.0


# ── permutation_test ──

def test_permutation_too_few_trades_returns_empty():
    assert permutation_test(np.array([1.0, -1.0]), CAPITAL) == {}


def test_permutation_pvalues_in_range_and_deterministic():
    rng = np.random.default_rng(7)
    pnl = rng.normal(0, 300, 60)
    a = permutation_test(pnl, CAPITAL, n_sims=500, seed=1)
    b = permutation_test(pnl, CAPITAL, n_sims=500, seed=1)
    scalars = [k for k in a if k != "sim_sharpes"]
    assert {k: a[k] for k in scalars} == {k: b[k] for k in scalars}
    assert np.array_equal(a["sim_sharpes"], b["sim_sharpes"])
    assert 0.0 <= a["p_value_sharpe"] <= 1.0
    assert 0.0 <= a["p_value_maxdd"] <= 1.0


def test_permutation_detects_engineered_bad_ordering():
    # All losses first, then all wins: the worst possible drawdown ordering.
    # Almost any shuffle produces a shallower max drawdown, so p ~ 1.
    pnl = np.concatenate([np.full(15, -400.0), np.full(15, 500.0)])
    r = permutation_test(pnl, CAPITAL, n_sims=300, seed=3)
    assert r["p_value_maxdd"] > 0.9


def test_permutation_random_ordering_is_unremarkable():
    # i.i.d. trades: the observed ordering is just one draw from the null,
    # so the p-value should sit well inside (0, 1), not at either extreme.
    rng = np.random.default_rng(11)
    pnl = rng.normal(50, 300, 80)
    r = permutation_test(pnl, CAPITAL, n_sims=500, seed=5)
    assert 0.05 < r["p_value_sharpe"] < 0.95


# ── bootstrap_sharpe_ci ──

def test_bootstrap_too_few_trades_returns_empty():
    assert bootstrap_sharpe_ci(np.array([1.0, 2.0, 3.0])) == {}


def test_bootstrap_observed_matches_direct_sharpe():
    pnl = np.array([100.0, -50.0, 200.0, -80.0, 40.0, 10.0])
    r = bootstrap_sharpe_ci(pnl, n_boot=100, seed=2)
    assert np.isclose(r["observed_sharpe"], pnl.mean() / pnl.std(ddof=1))


def test_bootstrap_ci_brackets_median_and_is_deterministic():
    rng = np.random.default_rng(23)
    pnl = rng.normal(20, 250, 100)
    a = bootstrap_sharpe_ci(pnl, n_boot=500, seed=9)
    b = bootstrap_sharpe_ci(pnl, n_boot=500, seed=9)
    scalars = [k for k in a if k != "boots"]
    assert {k: a[k] for k in scalars} == {k: b[k] for k in scalars}
    assert np.array_equal(a["boots"], b["boots"])
    assert a["ci_lower"] <= a["median_sharpe"] <= a["ci_upper"]
    assert 0.0 <= a["prob_positive"] <= 1.0


def test_bootstrap_strong_edge_has_high_prob_positive():
    rng = np.random.default_rng(31)
    pnl = rng.normal(300, 100, 60)   # mean 3x std: unmistakable edge
    r = bootstrap_sharpe_ci(pnl, n_boot=500, seed=4)
    assert r["prob_positive"] > 0.99
    assert r["ci_lower"] > 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
