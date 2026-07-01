"""
TradersPost webhook adapter.

TradersPost is a webhook → broker bridge: you POST an alert to your TradersPost
webhook URL and it executes the order on your connected (paper or live) broker.
Unlike a full broker API it does **not** return a synchronous fill price or live
account equity, so this adapter:

* ``place_order()`` POSTs a TradersPost-formatted payload and treats a 2xx ack
  as accepted, echoing the signalled price as the assumed fill.
* ``get_account()`` returns a *configured* equity (TradersPost doesn't expose it
  on the webhook path) so the margin guardrail still functions.

It implements the same interface as :class:`bot.broker.TradovateBroker`
(``get_account`` / ``place_order``), so the engine is unchanged. Transient
failures raise :class:`BrokerNetworkError` and flow through the same retry +
error-logging path as every other broker.
"""
from __future__ import annotations

from typing import Optional

import httpx

from .broker import (
    AccountSnapshot,
    BrokerNetworkError,
    BrokerRejectedError,
    OrderRequest,
    OrderResult,
)
from .contracts import get_contract


class TradersPostBroker:
    def __init__(self, webhook_url: str, account_equity: float,
                 stop_loss_points: float = 0.0, take_profit_points: float = 0.0,
                 client: Optional[httpx.Client] = None, timeout: float = 10.0):
        if not webhook_url:
            raise ValueError("TradersPost webhook URL is required")
        self.webhook_url = webhook_url
        self.account_equity = float(account_equity)
        self.stop_loss_points = float(stop_loss_points)
        self.take_profit_points = float(take_profit_points)
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ── account ─────────────────────────────────────────────────────
    def get_account(self) -> AccountSnapshot:
        # TradersPost's webhook path doesn't return equity; use the configured
        # value so the margin gate keeps working.
        return AccountSnapshot(
            equity=self.account_equity,
            available_margin=self.account_equity,
        )

    # ── orders ──────────────────────────────────────────────────────
    def _tick_round(self, symbol: str, price: float) -> float:
        try:
            tick = get_contract(symbol).tick_size
            return round(round(price / tick) * tick, 10)
        except Exception:
            return price

    def _bracket_price(self, req: OrderRequest, points: float,
                       protective: bool) -> Optional[float]:
        """Price ``points`` away from the signal. ``protective=True`` puts it on
        the losing side (stop); ``False`` on the winning side (take profit)."""
        if points <= 0:
            return None
        long = req.action == "buy"
        # stop: below for long / above for short. take-profit: opposite.
        below = (long == protective)
        raw = req.price - points if below else req.price + points
        return self._tick_round(req.symbol, raw)

    def _payload(self, req: OrderRequest) -> dict:
        """Map an internal OrderRequest to a TradersPost webhook payload."""
        payload = {
            "ticker": req.symbol,
            "action": req.action,          # buy | sell
            "quantity": req.quantity,
            "price": req.price,
            "type": req.order_type,        # market
        }
        stop = self._bracket_price(req, self.stop_loss_points, protective=True)
        if stop is not None:
            payload["stopLoss"] = {"type": "stop", "stopPrice": stop}
        tp = self._bracket_price(req, self.take_profit_points, protective=False)
        if tp is not None:
            payload["takeProfit"] = {"limitPrice": tp}
        return payload

    def place_order(self, req: OrderRequest) -> OrderResult:
        try:
            resp = self._client.post(self.webhook_url, json=self._payload(req))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BrokerNetworkError(
                f"network error posting to TradersPost: {exc}") from exc

        if resp.status_code >= 500:
            raise BrokerNetworkError(
                f"TradersPost {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise BrokerRejectedError(
                f"TradersPost rejected ({resp.status_code}): {resp.text[:200]}")

        # TradersPost acks receipt (sometimes with an id); it is not a fill.
        order_id = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                order_id = str(data.get("id") or data.get("orderId") or "")
        except ValueError:
            pass

        return OrderResult(
            broker_order_id=order_id or f"tp-{req.symbol}-{req.action}-{req.quantity}",
            status="accepted",
            fill_price=req.price,          # assumed fill = signalled price
            quantity=req.quantity,
        )
