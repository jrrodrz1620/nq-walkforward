"""
Shared pytest fixtures + mock network doubles.

``RecordingSleep`` makes backoff instant while recording the delays so tests can
assert the truncated-exponential schedule. ``FakeBroker`` is an in-process broker
double whose ``get_account``/``place_order`` can be scripted to succeed, reject,
or raise transient :class:`BrokerNetworkError`s (simulating network timeouts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from bot.broker import (
    AccountSnapshot,
    BrokerNetworkError,
    OrderRequest,
    OrderResult,
)
from bot.config import AppConfig, RetryConfig, RiskConfig
from bot.engine import TradingEngine
from bot.state import StateStore


class RecordingSleep:
    """A ``sleep`` substitute: records requested delays, never actually waits."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@dataclass
class FakeBroker:
    """Scriptable broker double (no real network)."""

    equity: float = 100_000.0
    available_margin: Optional[float] = None
    fill_price: Optional[float] = None
    # Raise BrokerNetworkError on the first N place_order calls, then succeed.
    fail_place_times: int = 0
    # Raise BrokerNetworkError on the first N get_account calls, then succeed.
    fail_account_times: int = 0
    place_calls: int = 0
    account_calls: int = 0
    orders: list[OrderRequest] = field(default_factory=list)
    _next_id: int = 1000

    def get_account(self) -> AccountSnapshot:
        self.account_calls += 1
        if self.account_calls <= self.fail_account_times:
            raise BrokerNetworkError("simulated account timeout")
        avail = self.available_margin
        if avail is None:
            avail = self.equity
        return AccountSnapshot(equity=self.equity, available_margin=avail)

    def place_order(self, req: OrderRequest) -> OrderResult:
        self.place_calls += 1
        if self.place_calls <= self.fail_place_times:
            raise BrokerNetworkError("simulated order timeout")
        self.orders.append(req)
        self._next_id += 1
        return OrderResult(
            broker_order_id=str(self._next_id),
            status="filled",
            fill_price=self.fill_price if self.fill_price is not None else req.price,
            quantity=req.quantity,
        )

    def close(self) -> None:  # parity with the real client
        pass


@pytest.fixture
def store() -> StateStore:
    s = StateStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def error_log_path(tmp_path) -> str:
    return str(tmp_path / "error_log.json")


@pytest.fixture
def fast_retry() -> RetryConfig:
    # Same schedule shape, tiny delays — RecordingSleep makes them free anyway.
    return RetryConfig(max_retries=3, base_delay=0.5, max_delay=8.0,
                       backoff_factor=2.0)


@pytest.fixture
def config(error_log_path, fast_retry) -> AppConfig:
    return AppConfig(
        passphrase="s3cret-pass",
        db_path=":memory:",
        error_log_path=error_log_path,
        retry=fast_retry,
        risk=RiskConfig(),
    )


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def sleeper() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
def engine(config, store, broker, sleeper) -> TradingEngine:
    return TradingEngine(config, store, broker, sleep=sleeper)


def make_payload(**overrides) -> dict:
    """A canonical TradingView webhook payload (override fields as needed)."""
    base = {
        "symbol": "ES",
        "action": "buy",
        "quantity": 1,
        "price": 5000.0,
        "passphrase": "s3cret-pass",
        "contract_month": "2025-06",
    }
    base.update(overrides)
    return base
