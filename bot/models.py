"""
Request/response models for the ``/webhook`` endpoint  (validation gate 1).

TradingView posts a JSON alert body. We accept the documented fields plus an
optional ``alert_id`` clients can supply for explicit idempotency.
"""
from __future__ import annotations

import hashlib
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


Action = Literal["buy", "sell"]


class WebhookPayload(BaseModel):
    symbol: str = Field(..., description="Root or month-coded ticker, e.g. ES, MNQM2025")
    action: Action = Field(..., description="buy or sell")
    quantity: int = Field(..., gt=0, description="Number of contracts (>0)")
    price: float = Field(..., gt=0, description="Signal/limit price in points")
    passphrase: str = Field(..., description="Shared secret validating the sender")
    contract_month: str = Field(..., description="Expiry code, e.g. 2025-06 or M2025")
    alert_id: Optional[str] = Field(
        default=None,
        description="Optional client-supplied id for explicit idempotency",
    )

    @field_validator("symbol", "contract_month")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be blank")
        return v.strip()

    @field_validator("action")
    @classmethod
    def _normalize_action(cls, v: str) -> str:
        return v.lower()

    def idempotency_key(self) -> str:
        """Stable key identifying a logical order.

        Uses the client-supplied ``alert_id`` when present; otherwise a hash of
        the trade-defining fields so identical, rapid-fire posts collapse to one.
        The passphrase is deliberately excluded.
        """
        if self.alert_id:
            return f"alert:{self.alert_id}"
        digest = hashlib.sha256(
            "|".join([
                self.symbol.upper(),
                self.action,
                str(self.quantity),
                f"{self.price:.10g}",
                self.contract_month,
            ]).encode()
        ).hexdigest()
        return f"hash:{digest}"


class WebhookResponse(BaseModel):
    status: str                      # filled | duplicate | rejected | error
    detail: Optional[str] = None
    order_id: Optional[int] = None
    broker_order_id: Optional[str] = None
    fill_price: Optional[float] = None
    idempotency_key: Optional[str] = None
