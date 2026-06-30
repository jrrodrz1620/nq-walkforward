"""
End-to-end engine pipeline: fills, idempotency, every rejection path, retry
success, and retry-exhaustion → error_log.json + graceful failure.
"""
import json
import os

from bot.models import WebhookPayload

from .conftest import make_payload


def _payload(**kw) -> WebhookPayload:
    return WebhookPayload(**make_payload(**kw))


def test_happy_path_fills_and_records(engine, broker, store):
    res = engine.process_webhook(_payload(symbol="ES", action="buy", quantity=1,
                                          price=5000.0))
    assert res.status == "filled"
    assert res.broker_order_id is not None
    assert store.net_contracts("ES", "2025-06") == 1
    assert len(broker.orders) == 1
    # Order row finalized as filled.
    assert store.get_order(res.order_id).status == "filled"


def test_idempotent_rapid_fire(engine, broker):
    p = _payload(alert_id="tv-123")
    first = engine.process_webhook(p)
    second = engine.process_webhook(p)
    assert first.status == "filled"
    assert second.status == "duplicate"
    # Only one order actually routed to the broker.
    assert broker.place_calls == 1


def test_position_cap_rejection(engine, store):
    engine.process_webhook(_payload(symbol="ES", quantity=2, alert_id="a1"))
    res = engine.process_webhook(_payload(symbol="ES", quantity=1, alert_id="a2"))
    assert res.status == "rejected"
    assert res.code == "position_cap"


def test_margin_rejection(config, store, sleeper):
    from bot.engine import TradingEngine
    from .conftest import FakeBroker
    broker = FakeBroker(equity=20_000, available_margin=20_000)  # < ES margin*2
    eng = TradingEngine(config, store, broker, sleep=sleeper)
    res = eng.process_webhook(_payload(symbol="ES", quantity=1, alert_id="m1"))
    assert res.status == "rejected"
    assert res.code == "margin"
    assert broker.place_calls == 0


def test_unknown_symbol_rejected_without_claim(engine):
    res = engine.process_webhook(_payload(symbol="ZZZ", alert_id="z1"))
    assert res.status == "rejected"
    assert res.code == "unknown_symbol"


def test_retry_then_success(config, store, sleeper):
    from bot.engine import TradingEngine
    from .conftest import FakeBroker
    broker = FakeBroker(fail_place_times=2)   # 2 transient failures, then fill
    eng = TradingEngine(config, store, broker, sleep=sleeper)
    res = eng.process_webhook(_payload(alert_id="r1"))
    assert res.status == "filled"
    assert broker.place_calls == 3
    assert sleeper.delays == [0.5, 1.0]       # two backoffs


def test_retry_exhaustion_logs_and_saves_state(config, store, sleeper,
                                               error_log_path):
    from bot.engine import TradingEngine
    from .conftest import FakeBroker
    broker = FakeBroker(fail_place_times=99)  # never succeeds
    eng = TradingEngine(config, store, broker, sleep=sleeper)
    res = eng.process_webhook(_payload(alert_id="e1"))

    assert res.status == "error"
    assert res.http_status == 503
    # 1 initial + 3 retries = 4 attempts.
    assert broker.place_calls == 4
    # error_log.json written with a state snapshot.
    assert os.path.exists(error_log_path)
    with open(error_log_path) as fh:
        entries = json.load(fh)
    assert len(entries) == 1
    assert entries[0]["severity"] == "critical"
    assert entries[0]["context"]["operation"] == "place_order"
    assert "state_snapshot" in entries[0]
    # Order finalized as failed; no phantom position.
    assert store.get_order(res.order_id).status == "failed"
    assert store.net_contracts("ES", "2025-06") == 0


def test_account_fetch_retry_exhaustion(config, store, sleeper, error_log_path):
    from bot.engine import TradingEngine
    from .conftest import FakeBroker
    broker = FakeBroker(fail_account_times=99)
    eng = TradingEngine(config, store, broker, sleep=sleeper)
    res = eng.process_webhook(_payload(alert_id="acc1"))
    assert res.status == "error"
    assert os.path.exists(error_log_path)


def test_daily_drawdown_halt_blocks_new_entry(engine, store):
    # Pre-load a losing day beyond the $1,000 limit.
    store.add_transaction("NQ", "2025-06", "sell", 1, 17000.0, -1_500.0)
    res = engine.process_webhook(_payload(symbol="ES", quantity=1, alert_id="dd1"))
    assert res.status == "rejected"
    assert res.code == "daily_drawdown"


def test_sell_then_buy_realizes_pnl(engine, broker, store):
    # Buy 1 ES @5000, then sell 1 ES @5010 → realized +$500.
    engine.process_webhook(_payload(symbol="ES", action="buy", quantity=1,
                                    price=5000.0, alert_id="t1"))
    broker.fill_price = 5010.0
    engine.process_webhook(_payload(symbol="ES", action="sell", quantity=1,
                                    price=5010.0, alert_id="t2"))
    assert store.net_contracts("ES", "2025-06") == 0
    assert store.realized_pnl_today() == 500.0
