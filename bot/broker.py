"""
Futures broker API wrapper  (validation gates 2 & 6).

A thin, Tradovate-style REST client plus a truncated exponential-backoff retry
helper. The client is deliberately small and dependency-light so tests can swap
in a mock transport; the retry helper isolates the "loop on transient network
failure, max 3 times" behaviour the spec asks for.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from .config import RetryConfig


class BrokerError(Exception):
    """Base class for broker failures."""


class BrokerNetworkError(BrokerError):
    """Transient/retryable failure (timeout, connection error, 5xx)."""


class BrokerRejectedError(BrokerError):
    """Broker actively rejected the order (non-retryable, e.g. 4xx)."""


@dataclass
class OrderRequest:
    symbol: str
    contract_month: str
    action: str           # buy | sell
    quantity: int
    price: float
    order_type: str = "market"

    @property
    def signed_quantity(self) -> int:
        return self.quantity if self.action == "buy" else -self.quantity


@dataclass
class OrderResult:
    broker_order_id: str
    status: str           # filled | working | rejected
    fill_price: float
    quantity: int


@dataclass
class AccountSnapshot:
    equity: float
    available_margin: float
    currency: str = "USD"


def with_retry(
    fn: Callable[[], "T"],
    config: RetryConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_giveup: Optional[Callable[[BrokerNetworkError, int], None]] = None,
) -> "T":
    """Run ``fn`` with truncated exponential backoff on :class:`BrokerNetworkError`.

    Makes up to ``1 + config.max_retries`` attempts. Delays are
    ``base_delay * factor**(attempt-1)`` capped at ``max_delay``. If every retry
    is exhausted, ``on_giveup(error, attempts)`` is invoked (for critical
    logging / state-saving) and the last error is re-raised.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except BrokerNetworkError as exc:
            attempt += 1
            if attempt > config.max_retries:
                if on_giveup is not None:
                    on_giveup(exc, attempt)
                raise
            delay = min(
                config.base_delay * (config.backoff_factor ** (attempt - 1)),
                config.max_delay,
            )
            sleep(delay)


class TradovateBroker:
    """Minimal REST broker client. Network calls funnel through ``_request`` so
    a single seam can be mocked in tests."""

    def __init__(self, base_url: str, token: str,
                 client: Optional[httpx.Client] = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ── low-level transport ─────────────────────────────────────────
    def _request(self, method: str, path: str,
                 json: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = self._client.request(method, url, json=json, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BrokerNetworkError(f"network error calling {path}: {exc}") from exc

        if resp.status_code >= 500:
            raise BrokerNetworkError(
                f"broker {resp.status_code} on {path}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            raise BrokerRejectedError(
                f"broker rejected {path} ({resp.status_code}): {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerError(f"invalid JSON from {path}: {exc}") from exc

    # ── high-level API ──────────────────────────────────────────────
    def get_account(self) -> AccountSnapshot:
        data = self._request("GET", "/account")
        return AccountSnapshot(
            equity=float(data["equity"]),
            available_margin=float(data.get("available_margin", data["equity"])),
            currency=data.get("currency", "USD"),
        )

    def place_order(self, req: OrderRequest) -> OrderResult:
        data = self._request("POST", "/orders", json={
            "symbol": req.symbol,
            "contractMonth": req.contract_month,
            "action": req.action,
            "orderQty": req.quantity,
            "price": req.price,
            "orderType": req.order_type,
        })
        return OrderResult(
            broker_order_id=str(data["orderId"]),
            status=data.get("status", "filled"),
            fill_price=float(data.get("fillPrice", req.price)),
            quantity=int(data.get("orderQty", req.quantity)),
        )
