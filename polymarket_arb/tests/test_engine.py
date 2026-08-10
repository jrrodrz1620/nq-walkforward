"""
End-to-end engine behaviour against fake feeds.

The Binance and Polymarket feeds are replaced with deterministic stand-ins, so
the whole path -- evaluate, size, risk-check, fill, persist, settle -- runs
without touching the network.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polymarket_arb.dashboard import NullDashboard
from polymarket_arb.engine import SETTLEMENT_GRACE_SECONDS, ArbEngine
from polymarket_arb.execution import PaperExecutor
from polymarket_arb.models import Position
from polymarket_arb.notifier import TelegramNotifier
from polymarket_arb.storage import TradeStore

from .conftest import SIGMA_50PCT, make_book, make_market


# ─────────────────────────────────────────────
# FAKE FEEDS
# ─────────────────────────────────────────────

class FakeBinance:
    def __init__(self, prices: dict[str, float], *, settlement: float | None = None):
        self.prices = prices
        self.settlement = settlement
        self.healthy = True
        self.settlement_calls = 0

    def price(self, asset): return self.prices.get(asset)
    def sigma(self, asset): return SIGMA_50PCT
    def vol_progress(self, asset): return 1.0
    def age(self, asset, now=None): return 0.0
    def is_healthy(self, asset=None): return self.healthy
    def status(self): return {"connected": True, "prices": self.prices}

    async def start(self): return None
    async def stop(self): return None
    async def wait_ready(self, timeout=30.0): return True

    async def window_open_price(self, asset, open_time): return self.prices.get(asset)

    async def settlement_price(self, asset, close_time):
        self.settlement_calls += 1
        return self.settlement


class FakePoly:
    def __init__(self, markets=(), books=None):
        self.markets = {m.condition_id: m for m in markets}
        self.books = books or {}
        self.healthy = True
        self.refreshes = 0

    def book(self, token_id): return self.books.get(token_id)

    def books_for(self, market):
        return {
            market.up_token_id: self.books.get(market.up_token_id),
            market.down_token_id: self.books.get(market.down_token_id),
        }

    def is_healthy(self): return self.healthy
    def status(self): return {"markets": len(self.markets), "books": len(self.books)}

    async def start(self): return None
    async def stop(self): return None
    async def discover(self): return list(self.markets.values())
    async def resolve_strikes(self, resolver): return 0

    async def refresh_books(self, markets):
        self.refreshes += 1
        return len(self.books)


def build_engine(cfg, *, markets=(), books=None, spot=100_150.0, settlement=None):
    binance = FakeBinance({"BTC": spot, "ETH": 3_000.0}, settlement=settlement)
    poly = FakePoly(markets, books)
    engine = ArbEngine(
        cfg,
        store=TradeStore(cfg.db_path),
        notifier=TelegramNotifier(cfg),
        binance=binance,
        polymarket=poly,
        executor=PaperExecutor(cfg),
        dashboard=NullDashboard(),
    )
    return engine


def lagging_books(market, *, size=5_000.0):
    """A book quoting UP at 0.60 when the model says it is worth ~0.92."""
    return {
        market.up_token_id: make_book(market.up_token_id, bid=0.59, ask=0.60, size=size),
        market.down_token_id: make_book(market.down_token_id, bid=0.40, ask=0.41, size=size),
    }


def fair_books(market):
    return {
        market.up_token_id: make_book(market.up_token_id, bid=0.91, ask=0.92),
        market.down_token_id: make_book(market.down_token_id, bid=0.08, ask=0.09),
    }


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_lagging_book_is_traded(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))

    await engine._scan_tick()      # first scan builds signal persistence
    await engine._scan_tick()

    assert len(engine.positions) == 1
    position = engine.positions[0]
    assert position.side == "UP"
    assert position.mode == "PAPER"
    assert position.id is not None
    assert engine.cash == pytest.approx(1_000.0 - position.cost_basis)
    assert engine.store.open_positions()[0].id == position.id
    assert engine.orders_sent == 1


@pytest.mark.asyncio
async def test_position_size_respects_the_eight_percent_cap(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))

    await engine._scan_tick()
    await engine._scan_tick()

    position = engine.positions[0]
    assert position.cost_basis <= cfg.max_position_pct * 1_000.0 + 0.01


@pytest.mark.asyncio
async def test_a_fairly_priced_book_is_left_alone(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=fair_books(market))

    for _ in range(3):
        await engine._scan_tick()

    assert engine.positions == []
    assert engine.orders_sent == 0


@pytest.mark.asyncio
async def test_signal_must_persist_before_it_is_traded(cfg):
    cfg.min_signal_ticks = 3
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))

    await engine._scan_tick()
    assert engine.positions == [], "a one-tick blip must not trade"
    for _ in range(3):
        await engine._scan_tick()
    assert len(engine.positions) == 1


@pytest.mark.asyncio
async def test_only_one_position_per_market(cfg):
    market = make_market(seconds_left=200.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))

    for _ in range(5):
        await engine._scan_tick()

    assert len(engine.positions) == 1


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_entries(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))
    engine.risk.halt("test halt")

    for _ in range(3):
        await engine._scan_tick()

    assert engine.positions == []
    kinds = [e["kind"] for e in engine.store.recent_events()]
    assert "risk_block" in kinds


@pytest.mark.asyncio
async def test_unhealthy_feeds_block_entries(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))
    engine.binance.healthy = False

    for _ in range(3):
        await engine._scan_tick()

    assert engine.positions == []


@pytest.mark.asyncio
async def test_a_thin_book_is_not_traded(cfg):
    market = make_market(seconds_left=150.0)
    # One share on offer: below both the dollar floor and the exchange minimum.
    engine = build_engine(cfg, markets=[market], books=lagging_books(market, size=1.0))

    for _ in range(3):
        await engine._scan_tick()

    assert engine.positions == []


@pytest.mark.asyncio
async def test_markets_without_a_strike_are_skipped(cfg):
    market = make_market(seconds_left=150.0, strike=None)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))
    await engine._scan_tick()
    assert engine.positions == []
    assert engine.latest_signals == []


# ─────────────────────────────────────────────
# SETTLEMENT AND EXITS
# ─────────────────────────────────────────────

async def _open_then(engine, market, *, close_time_offset: float | None = None):
    """Open a position, optionally repositioning its expiry."""
    await engine._scan_tick()
    await engine._scan_tick()
    assert engine.positions, "setup failed: no position was opened"
    position = engine.positions[0]
    if close_time_offset is not None:
        position.close_time = datetime.now(UTC) + timedelta(seconds=close_time_offset)
        market.close_time = position.close_time
    return position


@pytest.mark.asyncio
async def test_a_winning_contract_settles_at_one_dollar(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market),
                          settlement=100_400.0)
    position = await _open_then(engine, market, close_time_offset=-1.0)
    cash_before, shares = engine.cash, position.shares

    await engine._positions_tick()

    assert engine.positions == []
    assert position.exit_price == 1.0
    assert position.exit_reason == "settled"
    assert position.settlement_price == 100_400.0
    assert engine.cash == pytest.approx(cash_before + shares)
    assert engine.realized_total == pytest.approx(position.realized_pnl)

    stats = engine.store.stats()
    assert stats.total == 1 and stats.wins == 1 and stats.win_rate == 1.0


@pytest.mark.asyncio
async def test_a_losing_contract_settles_worthless(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market),
                          settlement=99_000.0)     # closed below the strike
    position = await _open_then(engine, market, close_time_offset=-1.0)
    cash_before = engine.cash

    await engine._positions_tick()

    assert position.exit_price == 0.0
    assert position.realized_pnl == pytest.approx(-position.cost_basis)
    assert engine.cash == pytest.approx(cash_before)
    assert engine.store.stats().losses == 1
    assert engine.risk.consecutive_losses == 1


@pytest.mark.asyncio
async def test_a_flat_close_resolves_down(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market),
                          settlement=100_000.0)    # exactly the strike
    position = await _open_then(engine, market, close_time_offset=-1.0)
    await engine._positions_tick()
    assert position.exit_price == 0.0, "'up' requires a strictly higher close"


@pytest.mark.asyncio
async def test_settlement_is_retried_before_the_grace_period_expires(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market),
                          settlement=None)         # price lookup keeps failing
    position = await _open_then(engine, market, close_time_offset=-1.0)

    await engine._positions_tick()
    assert engine.positions == [position], "position must stay open and retry"

    position.close_time = datetime.now(UTC) - timedelta(
        seconds=SETTLEMENT_GRACE_SECONDS + 1
    )
    await engine._positions_tick()

    assert engine.positions == []
    assert position.exit_reason == "settlement_unavailable"
    assert position.exit_price == pytest.approx(
        engine.poly.book(position.token_id).mid
    )


@pytest.mark.asyncio
async def test_take_profit_exits_into_the_bid(cfg):
    market = make_market(seconds_left=200.0)
    books = lagging_books(market)
    engine = build_engine(cfg, markets=[market], books=books)
    position = await _open_then(engine, market)

    # The book catches up to fair value, above the take-profit threshold.
    books[market.up_token_id] = make_book(market.up_token_id, bid=0.95, ask=0.96,
                                          size=5_000.0)
    await engine._positions_tick()

    assert engine.positions == []
    assert position.exit_reason == "take_profit"
    assert position.realized_pnl > 0


@pytest.mark.asyncio
async def test_stop_loss_exits_a_collapsing_position(cfg):
    market = make_market(seconds_left=200.0)
    books = lagging_books(market)
    engine = build_engine(cfg, markets=[market], books=books)
    position = await _open_then(engine, market)

    books[market.up_token_id] = make_book(market.up_token_id, bid=0.10, ask=0.12,
                                          size=5_000.0)
    await engine._positions_tick()

    assert position.exit_reason == "stop_loss"
    assert position.realized_pnl < 0


@pytest.mark.asyncio
async def test_no_early_exit_while_the_mark_is_between_the_thresholds(cfg):
    market = make_market(seconds_left=200.0)
    books = lagging_books(market)
    engine = build_engine(cfg, markets=[market], books=books)
    await _open_then(engine, market)

    books[market.up_token_id] = make_book(market.up_token_id, bid=0.70, ask=0.71)
    await engine._positions_tick()

    assert len(engine.positions) == 1


# ─────────────────────────────────────────────
# ACCOUNTING AND RISK
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_equity_tracks_cash_plus_marked_positions(cfg):
    market = make_market(seconds_left=200.0)
    books = lagging_books(market)
    engine = build_engine(cfg, markets=[market], books=books)
    position = await _open_then(engine, market)

    mark = books[market.up_token_id].mid
    assert engine.equity() == pytest.approx(engine.cash + position.shares * mark)
    assert engine.open_exposure() == pytest.approx(position.cost_basis)
    assert engine.unrealized() == pytest.approx(position.unrealized_pnl(mark))


@pytest.mark.asyncio
async def test_equity_tick_trips_the_kill_switch_and_records_it(cfg):
    engine = build_engine(cfg)
    engine.cash = 850.0                       # a 15% drawdown, past the 10% limit

    await engine._equity_tick()

    assert engine.risk.halted is True
    kinds = [e["kind"] for e in engine.store.recent_events()]
    assert "kill_switch" in kinds
    curve = engine.store.equity_curve()
    assert curve and curve[-1]["equity"] == pytest.approx(850.0)


@pytest.mark.asyncio
async def test_restored_positions_and_realized_pnl_rebuild_cash(cfg):
    store = TradeStore(cfg.db_path)
    held = Position(
        condition_id="0xold", market_label="BTC 5m", asset="BTC", window_minutes=5,
        side="UP", token_id="tok", shares=10.0, entry_price=0.50,
    )
    store.insert_position(held)
    closed = Position(
        condition_id="0xdone", market_label="BTC 5m", asset="BTC", window_minutes=5,
        side="UP", token_id="tok2", shares=10.0, entry_price=0.50,
    )
    store.insert_position(closed)
    closed.close(exit_price=1.0, reason="settled")     # +$5 realized
    store.close_position(closed)
    store.close()

    engine = build_engine(cfg)
    await engine._restore_state()

    assert len(engine.positions) == 1
    assert engine.realized_total == pytest.approx(5.0)
    # bankroll + realized - committed cost basis
    assert engine.cash == pytest.approx(1_000.0 + 5.0 - 5.0)


@pytest.mark.asyncio
async def test_tracked_markets_include_held_and_near_dated_only(cfg):
    near = make_market(seconds_left=120.0)
    far = make_market(asset="ETH", window=15, seconds_left=5_000.0)
    engine = build_engine(cfg, markets=[near, far], books=lagging_books(near))

    tracked = {m.condition_id for m in engine._tracked_markets()}
    assert near.condition_id in tracked
    assert far.condition_id not in tracked


@pytest.mark.asyncio
async def test_expired_markets_are_still_tracked_while_a_position_is_open(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))
    position = await _open_then(engine, market, close_time_offset=-5.0)

    tracked = {m.condition_id for m in engine._tracked_markets()}
    assert position.condition_id in tracked, "we must keep marking what we hold"


@pytest.mark.asyncio
async def test_dashboard_state_is_a_complete_snapshot(cfg):
    market = make_market(seconds_left=200.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market))
    await _open_then(engine, market)

    state = engine.build_state()
    assert state.mode == "PAPER"
    assert len(state.positions) == 1
    assert state.positions[0].side == "UP"
    assert state.signals, "signals feed the dashboard's scanner panel"
    assert state.equity == pytest.approx(engine.equity())
    assert state.markets_tracked == 1


@pytest.mark.asyncio
async def test_closed_trades_appear_in_the_last_ten(cfg):
    market = make_market(seconds_left=150.0)
    engine = build_engine(cfg, markets=[market], books=lagging_books(market),
                          settlement=100_400.0)
    await _open_then(engine, market, close_time_offset=-1.0)
    await engine._positions_tick()

    state = engine.build_state()
    assert len(state.trades) == 1
    assert state.trades[0].pnl > 0
    assert state.stats.win_rate == 1.0
    assert state.positions == []
