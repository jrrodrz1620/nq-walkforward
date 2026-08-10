"""Paper fills, and the guard that keeps the live executor out of paper runs."""
from __future__ import annotations

import pytest

from polymarket_arb.config import LIVE_CONFIRM_PHRASE, Config, Credentials, LiveTradingGate
from polymarket_arb.execution import (
    ExecutionError,
    LiveExecutor,
    PaperExecutor,
    build_executor,
)
from polymarket_arb.models import Position
from polymarket_arb.orderbook import BookSnapshot, Level

from .conftest import make_book


def position(**overrides) -> Position:
    data = dict(
        condition_id="0xabc", market_label="BTC 5m", asset="BTC", window_minutes=5,
        side="UP", token_id="tok", shares=20.0, entry_price=0.60,
    )
    data.update(overrides)
    return Position(**data)


# ─────────────────────────────────────────────
# PAPER BUYS
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_buy_walks_the_book_and_charges_slippage(cfg):
    cfg.slippage = 0.005
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.59, ask=0.60, size=100.0)

    fill = await executor.open(token_id="tok", shares=150.0, limit_price=0.63, book=book)

    # 100 shares at 0.60 and 50 at 0.61, plus the slippage allowance.
    expected_vwap = (100 * 0.60 + 50 * 0.61) / 150
    assert fill.status == "FILLED"
    assert fill.shares == 150.0
    assert fill.price == pytest.approx(expected_vwap + cfg.slippage)
    assert fill.filled is True
    assert fill.mode == "PAPER"


@pytest.mark.asyncio
async def test_paper_buy_partially_fills_at_the_limit(cfg):
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.59, ask=0.60, size=100.0)

    fill = await executor.open(token_id="tok", shares=250.0, limit_price=0.61, book=book)
    assert fill.status == "PARTIAL"
    assert fill.shares == 200.0


@pytest.mark.asyncio
async def test_paper_buy_through_the_book_does_not_fill(cfg):
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.59, ask=0.60)
    fill = await executor.open(token_id="tok", shares=10.0, limit_price=0.50, book=book)
    assert fill.filled is False
    assert fill.status == "UNFILLED"


@pytest.mark.asyncio
async def test_paper_buy_with_no_book_does_not_fill(cfg):
    executor = PaperExecutor(cfg)
    assert (await executor.open(token_id="t", shares=1.0, limit_price=0.5, book=None)).filled is False
    empty = BookSnapshot("t", bids=[Level(0.5, 10)], asks=[])
    assert (await executor.open(token_id="t", shares=1.0, limit_price=0.9,
                                book=empty)).filled is False


@pytest.mark.asyncio
async def test_fees_are_charged_on_notional(cfg):
    cfg.fee_bps = 100.0        # 1%
    cfg.slippage = 0.0
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.59, ask=0.60, size=1_000.0)
    fill = await executor.open(token_id="tok", shares=100.0, limit_price=0.60, book=book)
    assert fill.fees == pytest.approx(0.01 * fill.notional)
    assert fill.cost == pytest.approx(fill.notional + fill.fees)


# ─────────────────────────────────────────────
# PAPER SELLS
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_sell_hits_the_bid_less_slippage(cfg):
    cfg.slippage = 0.005
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.70, ask=0.72, size=100.0)

    fill = await executor.close(position=position(), shares=20.0, limit_price=0.65, book=book)
    assert fill.side == "SELL"
    assert fill.price == pytest.approx(0.70 - cfg.slippage)
    assert fill.status == "FILLED"


@pytest.mark.asyncio
async def test_paper_sell_below_the_limit_does_not_fill(cfg):
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.40, ask=0.42)
    fill = await executor.close(position=position(), shares=20.0, limit_price=0.60, book=book)
    assert fill.filled is False


@pytest.mark.asyncio
async def test_paper_sell_price_never_goes_negative(cfg):
    cfg.slippage = 0.5
    executor = PaperExecutor(cfg)
    book = make_book("tok", bid=0.02, ask=0.04, size=100.0)
    fill = await executor.close(position=position(), shares=10.0, limit_price=0.01, book=book)
    assert fill.price > 0.0


@pytest.mark.asyncio
async def test_paper_executor_has_nothing_to_cancel(cfg):
    assert await PaperExecutor(cfg).cancel_all() == 0


# ─────────────────────────────────────────────
# EXECUTOR SELECTION
# ─────────────────────────────────────────────

def test_paper_config_builds_a_paper_executor(cfg):
    executor = build_executor(cfg, gateway=None)   # a paper executor needs no gateway
    assert isinstance(executor, PaperExecutor)
    assert executor.mode == "PAPER"


def test_live_executor_refuses_to_exist_without_all_three_gates():
    cfg = Config(gate=LiveTradingGate(live_flag=True, risk_flag=True))   # no env phrase
    with pytest.raises(ExecutionError, match="refusing to build a live executor"):
        LiveExecutor(cfg, gateway=None)


def test_live_config_builds_a_live_executor():
    cfg = Config(
        gate=LiveTradingGate(True, True, LIVE_CONFIRM_PHRASE),
        credentials=Credentials(private_key="0xabc"),
    ).validate()
    assert isinstance(build_executor(cfg, gateway=None), LiveExecutor)


# ─────────────────────────────────────────────
# LIVE RESPONSE PARSING
# ─────────────────────────────────────────────

@pytest.fixture
def live_executor():
    cfg = Config(
        gate=LiveTradingGate(True, True, LIVE_CONFIRM_PHRASE),
        credentials=Credentials(private_key="0xabc"),
        fee_bps=0.0,
    ).validate()
    return LiveExecutor(cfg, gateway=None)


def test_matched_order_reports_the_traded_amounts(live_executor):
    fill = live_executor._parse_response(
        {"success": True, "status": "matched", "orderID": "0x1",
         "makingAmount": "12.0", "takingAmount": "20.0"},
        token_id="tok", side="BUY", requested=20.0, limit_price=0.62,
    )
    assert fill.status == "FILLED"
    assert fill.shares == 20.0
    assert fill.price == pytest.approx(0.60)


def test_partial_match_is_reported_as_partial(live_executor):
    fill = live_executor._parse_response(
        {"success": True, "status": "matched", "makingAmount": "6.0", "takingAmount": "10.0"},
        token_id="tok", side="BUY", requested=20.0, limit_price=0.62,
    )
    assert fill.status == "PARTIAL" and fill.shares == 10.0


def test_sell_side_amounts_are_read_the_other_way_round(live_executor):
    fill = live_executor._parse_response(
        {"success": True, "status": "matched", "makingAmount": "20.0", "takingAmount": "14.0"},
        token_id="tok", side="SELL", requested=20.0, limit_price=0.65,
    )
    assert fill.shares == 20.0
    assert fill.price == pytest.approx(0.70)


def test_rejected_order_is_not_a_fill(live_executor):
    fill = live_executor._parse_response(
        {"success": False, "errorMsg": "not enough balance"},
        token_id="tok", side="BUY", requested=20.0, limit_price=0.62,
    )
    assert fill.status == "REJECTED" and fill.filled is False
    assert "balance" in fill.detail


def test_unmatched_order_reports_no_fill(live_executor):
    fill = live_executor._parse_response(
        {"success": True, "status": "unmatched", "orderID": "0x9"},
        token_id="tok", side="BUY", requested=20.0, limit_price=0.62,
    )
    assert fill.filled is False and fill.status == "UNFILLED"


def test_a_resting_order_is_tracked_for_cancellation(live_executor):
    live_executor._parse_response(
        {"success": True, "status": "live", "orderID": "0xresting"},
        token_id="tok", side="BUY", requested=20.0, limit_price=0.62,
    )
    assert "0xresting" in live_executor.open_order_ids


def test_matched_without_amounts_falls_back_to_the_request(live_executor):
    fill = live_executor._parse_response(
        {"success": True, "status": "matched", "orderID": "0x2"},
        token_id="tok", side="BUY", requested=20.0, limit_price=0.62,
    )
    assert fill.shares == 20.0 and fill.price == pytest.approx(0.62)


def test_a_garbage_response_is_treated_as_unfilled(live_executor):
    fill = live_executor._parse_response(
        "gateway timeout", token_id="tok", side="BUY", requested=5.0, limit_price=0.5
    )
    assert fill.filled is False
