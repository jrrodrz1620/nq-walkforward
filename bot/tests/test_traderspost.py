"""
TradersPost adapter tests (mock network via httpx.MockTransport): payload
mapping, configured-equity account, valid ack, timeout, and 4xx rejection.
Plus a broker-factory test and an end-to-end engine fill through TradersPost.
"""
import httpx
import pytest

from bot.app import build_broker
from bot.broker import BrokerNetworkError, BrokerRejectedError, OrderRequest
from bot.config import AppConfig
from bot.engine import TradingEngine
from bot.models import WebhookPayload
from bot.state import StateStore
from bot.traderspost import TradersPostBroker

from .conftest import RecordingSleep, make_payload


def _broker(handler, equity=50_000.0) -> TradersPostBroker:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TradersPostBroker("https://webhooks.traderspost.io/trading/webhook/x/y",
                             equity, client=client)


def test_get_account_uses_configured_equity():
    acct = _broker(lambda r: httpx.Response(200), equity=75_000).get_account()
    assert acct.equity == 75_000
    assert acct.available_margin == 75_000


def test_place_order_posts_traderspost_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "tp-987"})

    res = _broker(handler).place_order(
        OrderRequest("ES", "2025-12", "buy", 2, 5000.0))
    # Mapped to TradersPost field names.
    assert seen == {"ticker": "ES", "action": "buy", "quantity": 2,
                    "price": 5000.0, "type": "market"}
    assert res.broker_order_id == "tp-987"
    assert res.fill_price == 5000.0


def test_stop_loss_attached_for_long():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    b = TradersPostBroker("https://webhooks.traderspost.io/x", 50_000,
                          stop_loss_points=40, client=client)
    b.place_order(OrderRequest("MES", "2025-12", "buy", 1, 5000.0))
    # MES tick 0.25; long stop 40 pts below entry.
    assert seen["stopLoss"] == {"type": "stop", "stopPrice": 4960.0}


def test_stop_loss_attached_for_short_and_tick_rounded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    b = TradersPostBroker("https://webhooks.traderspost.io/x", 50_000,
                          stop_loss_points=37.6, client=client)
    b.place_order(OrderRequest("MES", "2025-12", "sell", 1, 5000.0))
    # short stop above entry, snapped to 0.25 tick: 5037.6 -> 5037.5
    assert seen["stopLoss"]["stopPrice"] == 5037.5


def test_no_stop_when_disabled():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    b = TradersPostBroker("https://webhooks.traderspost.io/x", 50_000,
                          stop_loss_points=0, client=client)
    b.place_order(OrderRequest("MES", "2025-12", "buy", 1, 5000.0))
    assert "stopLoss" not in seen


def test_place_order_without_id_synthesizes_one():
    res = _broker(lambda r: httpx.Response(200, text="ok")).place_order(
        OrderRequest("MNQ", "2025-12", "sell", 1, 18000.0))
    assert res.broker_order_id == "tp-MNQ-sell-1"


def test_timeout_is_network_error():
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)
    with pytest.raises(BrokerNetworkError):
        _broker(handler).place_order(OrderRequest("ES", "2025-12", "buy", 1, 5000.0))


def test_4xx_is_rejection():
    with pytest.raises(BrokerRejectedError):
        _broker(lambda r: httpx.Response(401, text="bad token")).place_order(
            OrderRequest("ES", "2025-12", "buy", 1, 5000.0))


def test_5xx_is_retryable_network_error():
    with pytest.raises(BrokerNetworkError):
        _broker(lambda r: httpx.Response(502, text="bad gateway")).place_order(
            OrderRequest("ES", "2025-12", "buy", 1, 5000.0))


def test_build_broker_selects_traderspost():
    cfg = AppConfig(broker_type="traderspost",
                    traderspost_webhook_url="https://webhooks.traderspost.io/x")
    assert isinstance(build_broker(cfg), TradersPostBroker)


def test_build_broker_requires_url():
    cfg = AppConfig(broker_type="traderspost", traderspost_webhook_url="")
    with pytest.raises(RuntimeError):
        build_broker(cfg)


def test_engine_fills_through_traderspost(tmp_path):
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        posted.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "tp-1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    broker = TradersPostBroker("https://webhooks.traderspost.io/x", 100_000,
                               client=client)
    cfg = AppConfig(passphrase="s3cret-pass", db_path=":memory:",
                    error_log_path=str(tmp_path / "err.json"),
                    broker_type="traderspost",
                    traderspost_webhook_url="https://webhooks.traderspost.io/x",
                    account_equity=100_000)
    store = StateStore(":memory:")
    eng = TradingEngine(cfg, store, broker, sleep=RecordingSleep())

    res = eng.process_webhook(WebhookPayload(**make_payload(
        symbol="ES", action="buy", quantity=1, price=5000.0, alert_id="tp-e2e")))
    assert res.status == "filled"
    assert posted["ticker"] == "ES"
    assert store.net_contracts("ES", "2025-06") == 1
    store.close()
