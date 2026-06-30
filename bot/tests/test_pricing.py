import math

import pytest

from bot.contracts import get_contract
from bot.pricing import PricingEngine


@pytest.fixture
def es():
    return PricingEngine(get_contract("ES"))


def test_points_and_ticks(es):
    # ES tick = 0.25 points → a 10-point move is 40 ticks.
    assert es.to_points(10.0) == 10.0
    assert es.to_ticks(10.0) == 40.0
    assert es.ticks_to_points(40.0) == pytest.approx(10.0)


def test_dollar_value_uses_multiplier(es):
    # 10 points * $50 * 2 contracts = $1,000.
    assert es.dollar_value(10.0, quantity=2) == pytest.approx(1_000.0)


def test_pnl_long_and_short(es):
    assert es.pnl(5000, 5010, quantity=1, direction=1) == pytest.approx(500.0)
    assert es.pnl(5000, 5010, quantity=1, direction=-1) == pytest.approx(-500.0)


def test_round_to_tick(es):
    assert es.round_to_tick(5000.10) == pytest.approx(5000.0)
    assert es.round_to_tick(5000.13) == pytest.approx(5000.25)
    assert es.is_on_tick(5000.25)
    assert not es.is_on_tick(5000.10)


def test_required_margin(es):
    spec = es.spec
    assert es.required_margin(2) == pytest.approx(spec.initial_margin * 2)


def test_margin_check_pass(es):
    # 1 ES needs $13,200; with $100k equity at 50% cap, ceiling is $50k → ok.
    chk = es.check_margin(1, available_equity=100_000, max_utilization=0.5)
    assert chk.ok
    assert chk.required_margin == pytest.approx(13_200.0)
    assert chk.max_allowed == pytest.approx(50_000.0)


def test_margin_check_reject_over_threshold(es):
    # $20k equity, 50% cap → ceiling $10k < $13,200 required → reject.
    chk = es.check_margin(1, available_equity=20_000, max_utilization=0.5)
    assert not chk.ok
    assert chk.reason is not None
    assert "exceeds safe limit" in chk.reason


def test_margin_check_zero_equity(es):
    chk = es.check_margin(1, available_equity=0, max_utilization=0.5)
    assert not chk.ok
    assert math.isinf(chk.utilization)
