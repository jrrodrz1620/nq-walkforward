"""
Trading engine — orchestrates the full webhook → order pipeline.

This is where every validation gate meets: idempotency claim, mark update,
margin/risk gates, broker routing with exponential-backoff retry, state updates,
and critical-error logging. It is deliberately transport-agnostic (no FastAPI
here) so it can be unit-tested directly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .broker import (
    BrokerError,
    BrokerNetworkError,
    BrokerRejectedError,
    OrderRequest,
    TradovateBroker,
    with_retry,
)
from .config import AppConfig
from .contracts import UnknownContractError, get_contract
from .errors import save_error_state
from .models import WebhookPayload
from .risk import RiskManager
from .state import StateStore


@dataclass
class EngineResult:
    status: str                       # filled | duplicate | rejected | error
    http_status: int
    detail: Optional[str] = None
    code: Optional[str] = None
    order_id: Optional[int] = None
    broker_order_id: Optional[str] = None
    fill_price: Optional[float] = None
    idempotency_key: Optional[str] = None


class TradingEngine:
    def __init__(self, config: AppConfig, store: StateStore,
                 broker: TradovateBroker,
                 sleep: Callable[[float], None] = time.sleep,
                 notifier=None):
        self.config = config
        self.store = store
        self.broker = broker
        self.risk = RiskManager(config.risk, store)
        self._sleep = sleep
        self.notifier = notifier

    # ── public entrypoint ───────────────────────────────────────────
    def process_webhook(self, payload: WebhookPayload) -> EngineResult:
        result = self._process(payload)
        self._notify(payload, result)
        return result

    def _notify(self, payload: WebhookPayload, result: EngineResult) -> None:
        """Best-effort Telegram alert; never affects order handling."""
        if self.notifier is None or result.status == "duplicate":
            return
        try:
            from .notify import format_result
            self.notifier.send(format_result(payload, result))
        except Exception:
            pass

    def _process(self, payload: WebhookPayload) -> EngineResult:
        key = payload.idempotency_key()

        # Resolve contract first so an unknown symbol never claims a key.
        try:
            spec = get_contract(payload.symbol)
        except UnknownContractError as exc:
            return EngineResult(
                status="rejected", http_status=200, code="unknown_symbol",
                detail=str(exc), idempotency_key=key,
            )

        # ── idempotency: atomic claim ───────────────────────────────
        is_new, record = self.store.claim_order(
            key=key,
            symbol=spec.symbol,
            contract_month=payload.contract_month,
            action=payload.action,
            quantity=payload.quantity,
            price=payload.price,
            dedup_window_seconds=self.config.dedup_window_seconds,
        )
        if not is_new:
            return EngineResult(
                status="duplicate", http_status=200, code="duplicate",
                detail=f"order already seen (status={record.status})",
                order_id=record.id, broker_order_id=record.broker_order_id,
                fill_price=record.fill_price, idempotency_key=key,
            )

        order_id = record.id

        # Everything past the claim is wrapped so any failure both finalizes
        # the order row and persists a critical alert.
        try:
            return self._route(payload, spec, order_id, key)
        except (BrokerNetworkError,) as exc:
            return self._fail(order_id, key, exc, payload, "broker_unreachable")
        except BrokerRejectedError as exc:
            self.store.finalize_order(order_id, "rejected", reason=str(exc))
            return EngineResult(
                status="rejected", http_status=200, code="broker_rejected",
                detail=str(exc), order_id=order_id, idempotency_key=key,
            )
        except Exception as exc:  # global catch-all (gate 6)
            return self._fail(order_id, key, exc, payload, "unexpected_error")

    # ── internals ───────────────────────────────────────────────────
    def _route(self, payload: WebhookPayload, spec, order_id: int,
               key: str) -> EngineResult:
        # Mark the instrument at the signalled price for MtM risk math.
        self.store.update_mark(spec.symbol, payload.contract_month, payload.price)

        # Account equity is itself a network call → retry on transient errors.
        account = with_retry(
            self.broker.get_account, self.config.retry, sleep=self._sleep,
            on_giveup=lambda e, n: self._on_giveup(
                e, n, order_id, payload, "get_account"),
        )

        # ── risk gates ──────────────────────────────────────────────
        decision = self.risk.evaluate(
            spec=spec,
            contract_month=payload.contract_month,
            action=payload.action,
            quantity=payload.quantity,
            available_equity=account.available_margin,
        )
        if not decision.approved:
            self.store.finalize_order(order_id, "rejected", reason=decision.reason)
            return EngineResult(
                status="rejected", http_status=200, code=decision.code,
                detail=decision.reason, order_id=order_id, idempotency_key=key,
            )

        # ── route the order (retry on transient network failure) ────
        req = OrderRequest(
            symbol=spec.symbol,
            contract_month=payload.contract_month,
            action=payload.action,
            quantity=payload.quantity,
            price=payload.price,
        )
        result = with_retry(
            lambda: self.broker.place_order(req), self.config.retry,
            sleep=self._sleep,
            on_giveup=lambda e, n: self._on_giveup(
                e, n, order_id, payload, "place_order"),
        )

        # ── persist fill / position / transaction ───────────────────
        realized = self.store.apply_fill(
            symbol=spec.symbol,
            contract_month=payload.contract_month,
            signed_qty=req.signed_quantity,
            price=result.fill_price,
            multiplier=spec.multiplier,
        )
        self.store.update_mark(spec.symbol, payload.contract_month, result.fill_price)
        self.store.add_transaction(
            symbol=spec.symbol,
            contract_month=payload.contract_month,
            action=payload.action,
            quantity=payload.quantity,
            price=result.fill_price,
            realized_pnl=realized,
        )
        self.store.finalize_order(
            order_id, "filled",
            broker_order_id=result.broker_order_id,
            fill_price=result.fill_price,
        )
        return EngineResult(
            status="filled", http_status=200, code="filled",
            detail=f"filled {payload.quantity} {spec.symbol} @ {result.fill_price}",
            order_id=order_id, broker_order_id=result.broker_order_id,
            fill_price=result.fill_price, idempotency_key=key,
        )

    def _on_giveup(self, error: BrokerNetworkError, attempts: int,
                   order_id: int, payload: WebhookPayload, op: str) -> None:
        """Called once all retries are exhausted: log critical + save state."""
        save_error_state(
            self.config.error_log_path,
            error=error,
            context={
                "operation": op,
                "attempts": attempts,
                "order_id": order_id,
                "symbol": payload.symbol,
                "action": payload.action,
                "quantity": payload.quantity,
                "price": payload.price,
                "contract_month": payload.contract_month,
            },
            state_snapshot=self.store.snapshot(),
        )

    def _fail(self, order_id: int, key: str, error: BaseException,
              payload: WebhookPayload, code: str) -> EngineResult:
        self.store.finalize_order(order_id, "failed", reason=str(error))
        # _on_giveup already logged retry-exhaustion; log any other failure too.
        if not isinstance(error, BrokerNetworkError):
            save_error_state(
                self.config.error_log_path,
                error=error,
                context={"operation": code, "order_id": order_id,
                         "symbol": payload.symbol},
                state_snapshot=self.store.snapshot(),
            )
        return EngineResult(
            status="error", http_status=503, code=code,
            detail=f"{type(error).__name__}: {error}",
            order_id=order_id, idempotency_key=key,
        )
