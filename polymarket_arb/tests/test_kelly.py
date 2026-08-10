"""Half-Kelly sizing and the caps layered on top of it."""
from __future__ import annotations

import pytest

from polymarket_arb.kelly import (
    fractional_kelly,
    kelly_fraction_binary,
    round_shares,
    size_position,
)


# ─────────────────────────────────────────────
# KELLY FORMULA
# ─────────────────────────────────────────────

def test_kelly_is_edge_over_payoff():
    # p = 0.60 at a price of 0.50 -> (0.60 - 0.50) / (1 - 0.50) = 0.20
    assert kelly_fraction_binary(0.60, 0.50) == pytest.approx(0.20)


def test_kelly_is_zero_without_an_edge():
    assert kelly_fraction_binary(0.50, 0.50) == 0.0
    assert kelly_fraction_binary(0.40, 0.50) == 0.0


def test_kelly_grows_with_the_edge():
    small = kelly_fraction_binary(0.55, 0.50)
    large = kelly_fraction_binary(0.75, 0.50)
    assert 0 < small < large < 1.0


def test_a_certain_win_stakes_everything():
    assert kelly_fraction_binary(1.0, 0.5) == pytest.approx(1.0)


def test_half_kelly_is_exactly_half():
    full = kelly_fraction_binary(0.70, 0.40)
    assert fractional_kelly(0.70, 0.40, 0.5) == pytest.approx(full / 2)


@pytest.mark.parametrize("prob, price", [(1.5, 0.5), (-0.1, 0.5), (0.5, 0.0), (0.5, 1.0)])
def test_out_of_range_inputs_are_rejected(prob, price):
    with pytest.raises(ValueError):
        kelly_fraction_binary(prob, price)


def test_fraction_must_be_positive():
    with pytest.raises(ValueError):
        fractional_kelly(0.6, 0.5, 0.0)


def test_shares_round_down_to_the_exchange_precision():
    assert round_shares(12.3456) == 12.34
    assert round_shares(-1.0) == 0.0


# ─────────────────────────────────────────────
# SIZING
# ─────────────────────────────────────────────

def _size(cfg, **kwargs):
    params = dict(
        prob=0.90,
        price=0.60,
        equity=1_000.0,
        cfg=cfg,
        available_notional=10_000.0,
        exposure_budget=10_000.0,
        min_order_size=5.0,
    )
    params.update(kwargs)
    return size_position(**params)


def test_half_kelly_size_is_capped_by_the_position_limit(cfg):
    # Full Kelly here is (0.90 - 0.60) / 0.40 = 0.75; half-Kelly 0.375 of
    # equity, far above the 8% per-position cap.
    result = _size(cfg)
    assert result.ok is True
    assert result.kelly_full == pytest.approx(0.75)
    assert result.kelly_scaled == pytest.approx(0.375)
    assert result.binding_constraint == "max_position_pct"
    assert result.notional <= cfg.max_position_pct * 1_000.0 + 1e-9
    assert result.fraction_of_equity <= cfg.max_position_pct


def test_kelly_binds_when_the_edge_is_small(cfg):
    result = _size(cfg, prob=0.63, price=0.60)
    assert result.binding_constraint == "kelly"
    assert result.fraction_of_equity < cfg.max_position_pct


def test_book_depth_caps_the_size(cfg):
    result = _size(cfg, available_notional=20.0)
    assert result.binding_constraint == "book_depth"
    assert result.notional <= 20.0


def test_exposure_budget_caps_the_size(cfg):
    result = _size(cfg, exposure_budget=15.0)
    assert result.binding_constraint == "exposure_budget"
    assert result.notional <= 15.0


def test_no_edge_means_no_position(cfg):
    result = _size(cfg, prob=0.55, price=0.60)
    assert result.ok is False
    assert "no Kelly edge" in result.reason


def test_sizes_below_the_dollar_floor_are_refused(cfg):
    result = _size(cfg, equity=20.0)      # 8% of $20 is $1.60, under the $5 floor
    assert result.ok is False
    assert "below minimum" in result.reason


def test_exchange_minimum_order_size_is_respected(cfg):
    result = _size(cfg, equity=100.0, min_order_size=50.0)   # 50 shares * $0.60 = $30
    assert result.ok is False
    assert "below minimum" in result.reason


def test_zero_equity_is_refused(cfg):
    assert _size(cfg, equity=0.0).ok is False


def test_invalid_price_is_refused(cfg):
    assert _size(cfg, price=1.0).ok is False


def test_shares_and_notional_are_consistent(cfg):
    result = _size(cfg, equity=5_000.0)
    assert result.notional == pytest.approx(result.shares * result.price)
    assert result.shares == round_shares(result.shares)
    assert "capped by" in result.describe()


def test_quarter_kelly_is_half_the_half_kelly_size(cfg):
    half = _size(cfg, prob=0.63, price=0.60)
    cfg.kelly_fraction = 0.25
    quarter = _size(cfg, prob=0.63, price=0.60)
    assert quarter.kelly_scaled == pytest.approx(half.kelly_scaled / 2)
