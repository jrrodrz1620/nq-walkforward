"""Order book construction, depth arithmetic, and tick rounding."""
from __future__ import annotations

import pytest

from polymarket_arb.orderbook import BookSnapshot, Level, merge_books, round_to_tick


def book() -> BookSnapshot:
    """Asks 0.60/0.61/0.62 and bids 0.58/0.57, 100 shares each."""
    return BookSnapshot(
        token_id="tok",
        bids=[Level(0.58, 100), Level(0.57, 100)],
        asks=[Level(0.60, 100), Level(0.61, 100), Level(0.62, 100)],
    )


# ─────────────────────────────────────────────
# CONSTRUCTION
# ─────────────────────────────────────────────

def test_from_clob_sorts_both_sides_regardless_of_input_order():
    snapshot = BookSnapshot.from_clob("tok", {
        "bids": [{"price": "0.55", "size": "10"}, {"price": "0.58", "size": "10"}],
        "asks": [{"price": "0.62", "size": "10"}, {"price": "0.60", "size": "10"}],
        "timestamp": "1754827500000",
        "tick_size": "0.01",
    })
    assert snapshot.best_bid == 0.58
    assert snapshot.best_ask == 0.60
    assert snapshot.exchange_ts == pytest.approx(1_754_827_500.0)


def test_from_clob_accepts_objects_and_tuples():
    class Summary:
        bids = [(0.4, 5)]
        asks = [(0.5, 5)]
        timestamp = None
        tick_size = None

    snapshot = BookSnapshot.from_clob("tok", Summary())
    assert snapshot.mid == pytest.approx(0.45)
    assert snapshot.tick_size == 0.01


def test_malformed_levels_are_dropped_not_fatal():
    snapshot = BookSnapshot.from_clob("tok", {
        "bids": [{"price": "abc", "size": "1"}, {"price": "0.5", "size": "0"},
                 {"price": "1.5", "size": "3"}, {"price": "0.40", "size": "7"}],
        "asks": None,
    })
    assert [(l.price, l.size) for l in snapshot.bids] == [(0.40, 7.0)]
    assert snapshot.best_ask is None
    assert snapshot.is_two_sided is False


def test_one_sided_books_still_produce_a_mid():
    only_bids = BookSnapshot("tok", bids=[Level(0.3, 10)], asks=[])
    assert only_bids.mid == 0.3
    assert only_bids.spread is None
    assert BookSnapshot("tok").mid is None


# ─────────────────────────────────────────────
# TOP OF BOOK
# ─────────────────────────────────────────────

def test_top_of_book_metrics():
    b = book()
    assert b.best_bid == 0.58
    assert b.best_ask == 0.60
    assert b.mid == pytest.approx(0.59)
    assert b.spread == pytest.approx(0.02)
    assert b.is_two_sided is True


# ─────────────────────────────────────────────
# DEPTH
# ─────────────────────────────────────────────

def test_vwap_walks_multiple_levels():
    assert book().buy_vwap(50) == pytest.approx(0.60)
    assert book().buy_vwap(150) == pytest.approx((100 * 0.60 + 50 * 0.61) / 150)
    assert book().sell_vwap(150) == pytest.approx((100 * 0.58 + 50 * 0.57) / 150)


def test_vwap_is_none_when_depth_is_insufficient():
    assert book().buy_vwap(1_000) is None
    assert book().sell_vwap(0) is None


def test_partial_fill_respects_the_limit_price():
    filled, price = book().fill_buy(250, limit_price=0.61)
    assert filled == 200                       # 0.62 level is above the limit
    assert price == pytest.approx(0.605)


def test_fill_returns_nothing_when_the_limit_is_through_the_book():
    assert book().fill_buy(10, limit_price=0.59) == (0.0, 0.0)
    assert book().fill_sell(10, limit_price=0.59) == (0.0, 0.0)


def test_fill_without_a_limit_consumes_available_depth():
    filled, price = book().fill_buy(1_000)
    assert filled == 300
    assert price == pytest.approx((0.60 + 0.61 + 0.62) / 3)


def test_available_size_and_notional_at_a_limit():
    b = book()
    assert b.shares_available(0.61, side="BUY") == 200
    assert b.notional_available(0.61, side="BUY") == pytest.approx(121.0)
    assert b.shares_available(0.58, side="SELL") == 100
    assert b.total_depth_notional() == pytest.approx(
        0.58 * 100 + 0.57 * 100 + 0.60 * 100 + 0.61 * 100 + 0.62 * 100
    )


def test_merge_books_sums_depth():
    assert merge_books([book(), book()]) == pytest.approx(2 * book().total_depth_notional())


def test_age_uses_the_monotonic_receipt_time():
    b = book()
    b.received_at = 100.0
    assert b.age(now=105.0) == pytest.approx(5.0)


# ─────────────────────────────────────────────
# TICK ROUNDING
# ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "price, tick, mode, expected",
    [
        (0.6149, 0.01, "nearest", 0.61),
        (0.6149, 0.01, "up", 0.62),
        (0.6149, 0.01, "down", 0.61),
        (0.30000000000000004, 0.01, "nearest", 0.30),
        (0.1234, 0.001, "up", 0.124),
    ],
)
def test_round_to_tick(price, tick, mode, expected):
    assert round_to_tick(price, tick, mode=mode) == pytest.approx(expected)


def test_round_to_tick_clamps_inside_the_unit_interval():
    # The CLOB rejects 0 and 1, so rounding must never reach either.
    assert round_to_tick(1.5, 0.01, mode="up") == 0.99
    assert round_to_tick(0.0001, 0.01, mode="down") == 0.01


def test_round_to_tick_is_a_no_op_without_a_tick():
    assert round_to_tick(0.4321, 0.0) == 0.4321
