"""Fair value, volatility estimation, confidence scoring, and the trade gates."""
from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from polymarket_arb.pricing import (
    RealizedVol,
    composite_confidence,
    confidence_components,
    evaluate_market,
    norm_cdf,
    prob_up,
)

from .conftest import SIGMA_50PCT, make_book, make_market


# ─────────────────────────────────────────────
# PROBABILITY
# ─────────────────────────────────────────────

def test_norm_cdf_is_symmetric():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.0) + norm_cdf(-1.0) == pytest.approx(1.0)


def test_at_the_money_is_a_coin_flip():
    assert prob_up(100.0, 100.0, SIGMA_50PCT, 300.0) == pytest.approx(0.5)


def test_probability_rises_with_spot():
    below = prob_up(99_900.0, 100_000.0, SIGMA_50PCT, 150.0)
    above = prob_up(100_100.0, 100_000.0, SIGMA_50PCT, 150.0)
    assert below < 0.5 < above
    # Symmetric moves in log space give symmetric probabilities.
    assert below + prob_up(100_000.0 ** 2 / 99_900.0, 100_000.0, SIGMA_50PCT, 150.0) \
        == pytest.approx(1.0)


def test_probability_decays_toward_certainty_as_expiry_approaches():
    far = prob_up(100_150.0, 100_000.0, SIGMA_50PCT, 280.0)
    near = prob_up(100_150.0, 100_000.0, SIGMA_50PCT, 20.0)
    assert 0.5 < far < near < 1.0


def test_expired_and_zero_vol_contracts_resolve_deterministically():
    assert prob_up(101.0, 100.0, SIGMA_50PCT, 0.0) == 1.0
    assert prob_up(99.0, 100.0, SIGMA_50PCT, -5.0) == 0.0
    assert prob_up(101.0, 100.0, 0.0, 300.0) == 1.0


def test_non_positive_prices_are_rejected():
    with pytest.raises(ValueError):
        prob_up(0.0, 100.0, SIGMA_50PCT, 60.0)


# ─────────────────────────────────────────────
# REALIZED VOLATILITY
# ─────────────────────────────────────────────

def test_vol_needs_a_warmup_before_it_is_trusted():
    vol = RealizedVol(warmup_samples=5, min_sample_interval=0.0)
    assert vol.ready is False
    for i in range(1, 7):
        vol.update(float(i), 100.0 * math.exp(0.0001 * ((-1) ** i)))
    assert vol.ready is True
    assert vol.warmup_progress() == 1.0


def test_vol_tracks_the_size_of_returns():
    calm = RealizedVol(warmup_samples=1, min_sample_interval=0.0, halflife_samples=5)
    wild = RealizedVol(warmup_samples=1, min_sample_interval=0.0, halflife_samples=5)
    price_calm = price_wild = 100.0
    for i in range(1, 200):
        price_calm *= math.exp(0.00001 * ((-1) ** i))
        price_wild *= math.exp(0.001 * ((-1) ** i))
        calm.update(float(i), price_calm)
        wild.update(float(i), price_wild)
    assert wild.sigma_per_sqrt_sec > calm.sigma_per_sqrt_sec
    assert wild.annualized > calm.annualized


def test_vol_throttles_samples_below_the_minimum_interval():
    vol = RealizedVol(min_sample_interval=1.0, warmup_samples=1)
    vol.update(0.0, 100.0)
    vol.update(0.1, 101.0)     # too soon, ignored
    assert vol.samples == 0
    vol.update(1.5, 101.0)
    assert vol.samples == 1


def test_vol_is_clamped_to_the_sanity_band():
    vol = RealizedVol(warmup_samples=1, min_sample_interval=0.0, ceiling=1e-4)
    vol.update(0.0, 100.0)
    vol.update(1.0, 200.0)     # a 100% move in one second
    assert vol.sigma_per_sqrt_sec == 1e-4


def test_vol_ignores_non_positive_prices():
    vol = RealizedVol(warmup_samples=1, min_sample_interval=0.0)
    vol.update(0.0, 100.0)
    vol.update(1.0, 0.0)
    assert vol.samples == 0


# ─────────────────────────────────────────────
# CONFIDENCE
# ─────────────────────────────────────────────

def _ideal_components(**overrides):
    base = dict(
        binance_age=0.0,
        clob_age=0.0,
        max_binance_age=2.0,
        max_clob_age=5.0,
        spread=0.0,
        reference_spread=0.04,
        depth_notional=1_000.0,
        reference_notional=100.0,
        vol_progress=1.0,
        seconds_left=150.0,
        min_seconds=45.0,
        max_seconds=300.0,
        persistence_ticks=3,
        required_ticks=2,
        divergence=0.10,
        min_divergence=0.03,
    )
    base.update(overrides)
    return confidence_components(**base)


def test_ideal_conditions_score_near_one():
    assert composite_confidence(_ideal_components()) > 0.98


def test_a_stale_feed_destroys_confidence():
    stale = _ideal_components(binance_age=2.5)
    assert stale["freshness"] == 0.0
    assert composite_confidence(stale) == 0.0


def test_an_empty_book_destroys_confidence():
    assert composite_confidence(_ideal_components(depth_notional=0.0)) == 0.0
    assert composite_confidence(_ideal_components(spread=None)) == 0.0


def test_confidence_falls_off_at_the_edges_of_the_window():
    mid = composite_confidence(_ideal_components(seconds_left=170.0))
    edge = composite_confidence(_ideal_components(seconds_left=60.0))
    assert 0.0 < edge < mid


def test_persistence_and_margin_scale_confidence():
    weak = _ideal_components(persistence_ticks=1, required_ticks=4, divergence=0.031)
    assert composite_confidence(weak) < composite_confidence(_ideal_components())


def test_confidence_is_geometric_not_arithmetic():
    # One zero component must sink the whole score, which an average would not.
    assert composite_confidence(_ideal_components(vol_progress=0.0)) == 0.0


# ─────────────────────────────────────────────
# EVALUATION
# ─────────────────────────────────────────────

def _books(market, *, up_bid=0.79, up_ask=0.80, down_bid=0.20, down_ask=0.21, size=500.0):
    return {
        market.up_token_id: make_book(market.up_token_id, bid=up_bid, ask=up_ask, size=size),
        market.down_token_id: make_book(market.down_token_id, bid=down_bid, ask=down_ask,
                                        size=size),
    }


def _evaluate(cfg, market, spot, books=None, **kwargs):
    params = dict(
        cfg=cfg,
        spot=spot,
        sigma=SIGMA_50PCT,
        books=books if books is not None else _books(market),
        binance_age=0.0,
        vol_progress=1.0,
        reference_notional=80.0,
        persistence={f"{market.condition_id}:UP": 3, f"{market.condition_id}:DOWN": 3},
        now=datetime.now(UTC),
    )
    params.update(kwargs)
    return evaluate_market(market, **params)


def test_a_lagging_book_produces_a_tradeable_signal(cfg):
    market = make_market(seconds_left=150.0)
    result = _evaluate(cfg, market, spot=100_150.0)

    assert result is not None
    assert result.side == "UP"                      # spot is above the strike
    assert result.fair_prob > 0.9
    assert result.divergence > cfg.min_divergence
    assert result.edge > cfg.min_edge
    assert result.confidence > cfg.min_confidence
    assert result.tradeable is True
    assert result.reason == "tradeable"


def test_a_fairly_priced_book_is_not_tradeable(cfg):
    market = make_market(seconds_left=150.0)
    result = _evaluate(cfg, market, spot=100_150.0, books=_books(market, up_bid=0.91,
                                                                up_ask=0.92,
                                                                down_bid=0.08,
                                                                down_ask=0.09))
    assert result.tradeable is False
    assert "divergence" in result.reason


def test_the_cheaper_side_wins_when_the_book_lags_downward(cfg):
    market = make_market(seconds_left=150.0)
    # Spot below the strike, but the book still prices UP richly.
    result = _evaluate(cfg, market, spot=99_850.0)
    assert result.side == "DOWN"
    assert result.tradeable is True


def test_market_without_a_strike_cannot_be_evaluated(cfg):
    market = make_market(strike=None)
    assert _evaluate(cfg, market, spot=100_150.0) is None


def test_expiring_market_is_blocked_by_the_time_gate(cfg):
    market = make_market(seconds_left=20.0)
    result = _evaluate(cfg, market, spot=100_150.0)
    assert result.tradeable is False
    assert "expiry" in result.reason


def test_missing_books_yield_no_evaluation(cfg):
    market = make_market()
    empty = {market.up_token_id: None, market.down_token_id: None}
    assert _evaluate(cfg, market, spot=100_150.0, books=empty) is None


def test_low_confidence_blocks_an_otherwise_good_edge(cfg):
    market = make_market(seconds_left=150.0)
    result = _evaluate(cfg, market, spot=100_150.0, vol_progress=0.2)
    assert result.edge > cfg.min_edge          # the edge is still there ...
    assert result.tradeable is False           # ... but we do not trust it
    assert "confidence" in result.reason


def test_slippage_and_fees_are_charged_to_the_edge(cfg):
    market = make_market(seconds_left=150.0)
    cheap = _evaluate(cfg, market, spot=100_150.0)
    cfg.fee_bps = 200.0
    cfg.slippage = 0.05
    pricey = _evaluate(cfg, market, spot=100_150.0)
    assert pricey.effective_cost > cheap.effective_cost
    assert pricey.edge < cheap.edge
