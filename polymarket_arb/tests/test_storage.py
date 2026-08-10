"""SQLite persistence: positions, full history, fills, equity, and stats."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polymarket_arb.models import CLOSED, OPEN, Fill, Position, TradeStats
from polymarket_arb.storage import TradeStore


@pytest.fixture
def store(tmp_path):
    with TradeStore(tmp_path / "nested" / "trades.sqlite3") as store:
        yield store


def make_position(**overrides) -> Position:
    data = dict(
        condition_id="0xabc",
        market_label="BTC 5m @ 12:05:00",
        asset="BTC",
        window_minutes=5,
        side="UP",
        token_id="111",
        shares=20.0,
        entry_price=0.60,
        entry_fees=0.05,
        close_time=datetime(2026, 8, 10, 12, 5, tzinfo=UTC),
        strike=100_000.0,
        spot_at_entry=100_150.0,
        fair_prob=0.91,
        market_mid=0.60,
        divergence=0.31,
        edge=0.50,
        confidence=0.93,
        mode="PAPER",
    )
    data.update(overrides)
    return Position(**data)


# ─────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────

def test_insert_assigns_an_id_and_opens_the_history(store):
    position = make_position()
    position_id = store.insert_position(position)

    assert position_id > 0 and position.id == position_id
    assert [p.id for p in store.open_positions()] == [position_id]

    history = store.position_history(position_id)
    assert [h["event"] for h in history] == ["OPEN"]
    assert history[0]["mark_price"] == pytest.approx(0.60)


def test_round_trip_preserves_the_decision_context(store):
    original = make_position()
    store.insert_position(original)
    restored = store.open_positions()[0]

    assert restored.fair_prob == pytest.approx(original.fair_prob)
    assert restored.confidence == pytest.approx(original.confidence)
    assert restored.strike == pytest.approx(original.strike)
    assert restored.entry_time.tzinfo is not None
    assert restored.close_time == original.close_time


def test_closing_a_position_records_pnl_and_history(store):
    position = make_position()
    store.insert_position(position)
    pnl = position.close(exit_price=1.0, reason="settled", settlement_price=100_200.0)
    store.close_position(position, spot=100_200.0)

    assert store.open_positions() == []
    closed = store.recent_trades(10)
    assert len(closed) == 1
    assert closed[0].status == CLOSED
    assert closed[0].realized_pnl == pytest.approx(pnl)
    assert closed[0].exit_reason == "settled"

    events = [h["event"] for h in store.position_history(position.id)]
    assert events == ["OPEN", "CLOSE"]


def test_mark_history_accumulates(store):
    position = make_position()
    store.insert_position(position)
    store.bulk_record_history([(position, 0.70, 100_200.0), (position, 0.75, 100_250.0)])
    history = store.position_history(position.id)
    assert [h["event"] for h in history] == ["OPEN", "MARK", "MARK"]
    # Unrealized P&L is stored alongside each mark.
    assert history[-1]["unrealized"] == pytest.approx(position.unrealized_pnl(0.75))


def test_history_for_an_unsaved_position_is_a_no_op(store):
    store.record_history(make_position(), event="MARK", mark_price=0.5)   # no id, no crash


def test_recent_trades_are_newest_first_and_limited(store):
    base = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    for i in range(12):
        position = make_position(condition_id=f"0x{i}")
        store.insert_position(position)
        position.close(exit_price=1.0 if i % 2 else 0.0, reason="settled",
                       at=base + timedelta(minutes=i))
        store.close_position(position)

    recent = store.recent_trades(10)
    assert len(recent) == 10
    assert recent[0].exit_time > recent[-1].exit_time


# ─────────────────────────────────────────────
# FILLS, EQUITY, EVENTS
# ─────────────────────────────────────────────

def test_fills_are_recorded_with_and_without_a_position(store):
    position = make_position()
    store.insert_position(position)
    store.record_fill(Fill("111", "BUY", 20.0, 0.60, fees=0.05), position.id)
    rejected = Fill("111", "BUY", 0.0, 0.0, status="REJECTED", detail="no liquidity")
    assert store.record_fill(rejected) > 0

    rows = store.conn.execute("SELECT status, position_id FROM fills ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["FILLED", "REJECTED"]
    assert rows[0][1] == position.id and rows[1][1] is None


def test_equity_curve_is_chronological(store):
    for equity in (1_000.0, 1_010.0, 995.0):
        store.record_equity(equity=equity, cash=equity, open_exposure=0.0, unrealized=0.0,
                            realized_total=0.0, daily_drawdown=0.0, open_positions=0,
                            halted=False)
    curve = store.equity_curve()
    assert [row["equity"] for row in curve] == [1_000.0, 1_010.0, 995.0]


def test_events_are_stored_newest_first_with_payloads(store):
    store.record_event("kill_switch", "drawdown breached", severity="critical",
                       payload={"drawdown": 0.11})
    store.record_event("startup", "engine starting")
    events = store.recent_events()
    assert events[0]["kind"] == "startup"
    assert events[1]["severity"] == "critical"
    assert "0.11" in events[1]["payload"]


def test_unserialisable_payloads_do_not_break_logging(store):
    store.record_event("odd", "message", payload={"when": datetime.now(UTC)})
    assert store.recent_events()[0]["payload"]


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────

def _close(store, pnl_price: float, entry: float = 0.60):
    position = make_position(entry_price=entry, entry_fees=0.0)
    store.insert_position(position)
    position.close(exit_price=pnl_price, reason="settled")
    store.close_position(position)
    return position


def test_stats_summarise_wins_and_losses(store):
    _close(store, 1.0)      # +$8 on 20 shares bought at 0.60
    _close(store, 1.0)
    _close(store, 0.0)      # -$12

    stats = store.stats()
    assert stats.total == 3
    assert stats.wins == 2 and stats.losses == 1
    assert stats.win_rate == pytest.approx(2 / 3)
    assert stats.net_pnl == pytest.approx(8.0 + 8.0 - 12.0)
    assert stats.profit_factor == pytest.approx(16.0 / 12.0)
    assert stats.best == pytest.approx(8.0)
    assert stats.worst == pytest.approx(-12.0)
    assert store.total_realized() == pytest.approx(stats.net_pnl)
    assert store.counts() == {CLOSED: 3}


def test_stats_can_be_scoped_to_a_time_window(store):
    _close(store, 1.0)
    future = datetime.now(UTC) + timedelta(hours=1)
    assert store.stats(since=future).total == 0


def test_empty_store_has_neutral_stats(store):
    stats = store.stats()
    assert stats == TradeStats()
    assert stats.win_rate == 0.0
    assert stats.profit_factor == 0.0
    assert stats.expectancy == 0.0


def test_trade_stats_handle_a_lossless_record():
    stats = TradeStats.from_pnls([5.0, 5.0])
    assert stats.profit_factor == float("inf")
    assert stats.avg_win == 5.0 and stats.avg_loss == 0.0
    assert stats.expectancy == pytest.approx(5.0)


def test_breakeven_trades_are_counted_separately():
    stats = TradeStats.from_pnls([0.0, 1.0, -1.0])
    assert stats.breakeven == 1
    assert stats.win_rate == pytest.approx(0.5)


# ─────────────────────────────────────────────
# POSITION MODEL
# ─────────────────────────────────────────────

def test_position_economics():
    position = make_position(shares=10.0, entry_price=0.40, entry_fees=0.10)
    assert position.cost_basis == pytest.approx(4.10)
    assert position.market_value(0.50) == pytest.approx(5.0)
    assert position.unrealized_pnl(0.50) == pytest.approx(0.90)
    assert position.unrealized_pct(0.50) == pytest.approx(0.90 / 4.10)
    assert position.payoff_if_correct() == pytest.approx(10.0 - 4.10)
    assert position.status == OPEN and position.won is None


def test_closing_computes_realized_pnl_net_of_fees():
    position = make_position(shares=10.0, entry_price=0.40, entry_fees=0.0)
    pnl = position.close(exit_price=1.0, reason="settled", fees=0.25)
    assert pnl == pytest.approx(10.0 - 4.0 - 0.25)
    assert position.won is True and position.is_open is False
