"""
Broker-layer tests using httpx.MockTransport as the mock network object:
valid API executions, network timeouts, and the retry/backoff helper.
"""
import httpx
import pytest

from bot.broker import (
    BrokerNetworkError,
    BrokerRejectedError,
    OrderRequest,
    TradovateBroker,
    with_retry,
)
from bot.config import RetryConfig

from .conftest import RecordingSleep


def _broker(handler) -> TradovateBroker:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return TradovateBroker("https://broker.test/v1", "tok", client=client)


def test_get_account_valid():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"equity": 50000, "available_margin": 48000})

    acct = _broker(handler).get_account()
    assert acct.equity == 50000
    assert acct.available_margin == 48000


def test_place_order_valid_execution():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/orders")
        return httpx.Response(200, json={
            "orderId": 777, "status": "filled", "fillPrice": 5000.25, "orderQty": 1,
        })

    res = _broker(handler).place_order(
        OrderRequest("ES", "2025-06", "buy", 1, 5000.0))
    assert res.broker_order_id == "777"
    assert res.fill_price == 5000.25
    assert res.status == "filled"


def test_network_timeout_becomes_broker_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(BrokerNetworkError):
        _broker(handler).get_account()


def test_5xx_is_retryable_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with pytest.raises(BrokerNetworkError):
        _broker(handler).place_order(OrderRequest("ES", "2025-06", "buy", 1, 5000.0))


def test_4xx_is_non_retryable_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad symbol")

    with pytest.raises(BrokerRejectedError):
        _broker(handler).place_order(OrderRequest("ZZ", "2025-06", "buy", 1, 1.0))


# ── retry helper ────────────────────────────────────────────────────
def test_with_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise BrokerNetworkError("transient")
        return "ok"

    sleeper = RecordingSleep()
    out = with_retry(fn, RetryConfig(max_retries=3, base_delay=0.5), sleep=sleeper)
    assert out == "ok"
    assert calls["n"] == 3
    # Two backoffs before the 3rd success: 0.5, 1.0.
    assert sleeper.delays == [0.5, 1.0]


def test_with_retry_truncated_exponential_schedule():
    sleeper = RecordingSleep()

    def always_fail():
        raise BrokerNetworkError("nope")

    giveups = []
    with pytest.raises(BrokerNetworkError):
        with_retry(always_fail,
                   RetryConfig(max_retries=3, base_delay=0.5, max_delay=8.0,
                               backoff_factor=2.0),
                   sleep=sleeper,
                   on_giveup=lambda e, n: giveups.append(n))
    # 3 retries → 3 sleeps: 0.5, 1.0, 2.0, then give up.
    assert sleeper.delays == [0.5, 1.0, 2.0]
    assert giveups == [4]


def test_with_retry_respects_max_delay_ceiling():
    sleeper = RecordingSleep()

    def always_fail():
        raise BrokerNetworkError("nope")

    with pytest.raises(BrokerNetworkError):
        with_retry(always_fail,
                   RetryConfig(max_retries=5, base_delay=1.0, max_delay=4.0,
                               backoff_factor=2.0),
                   sleep=sleeper)
    # 1, 2, 4, 4(capped), 4(capped)
    assert sleeper.delays == [1.0, 2.0, 4.0, 4.0, 4.0]
