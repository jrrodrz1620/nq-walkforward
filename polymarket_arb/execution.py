"""
Order execution: a simulated executor for paper mode and a CLOB executor for live.

Both implement the same small interface -- `open`, `close`, `settle`, `cancel_all`
-- so the engine is identical in either mode and paper results stay comparable
to live ones.

Paper fills walk the real order book and then apply `cfg.slippage` on top. That
extra allowance is deliberate: by the time an order actually reaches the CLOB,
the book we scored has already moved, and the edge calculation charges the same
allowance, so simulated P&L stays consistent with the edge the bot thought it
was capturing.

Live orders are posted as fill-or-kill marketable limits. A latency arbitrage
that rests on the book is no longer a latency arbitrage -- if it does not fill
immediately at our price, the opportunity is gone and the order should die
rather than linger as unwanted exposure.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from .config import Config
from .feeds.polymarket import ClobGateway
from .models import Fill, Position, utc_now
from .orderbook import BookSnapshot, round_to_tick

log = logging.getLogger(__name__)

#: Never send a price outside this band; the CLOB rejects 0 and 1.
MIN_PRICE = 0.001
MAX_PRICE = 0.999


class ExecutionError(RuntimeError):
    """An order could not be placed or cancelled."""


class Executor(Protocol):
    """Interface shared by the paper and live executors."""

    mode: str

    async def open(
        self, *, token_id: str, shares: float, limit_price: float, book: BookSnapshot | None
    ) -> Fill: ...

    async def close(
        self, *, position: Position, shares: float, limit_price: float,
        book: BookSnapshot | None,
    ) -> Fill: ...

    async def cancel_all(self) -> int: ...


def _fee(notional: float, cfg: Config) -> float:
    return abs(notional) * (cfg.fee_bps / 10_000.0)


# ─────────────────────────────────────────────
# PAPER
# ─────────────────────────────────────────────

class PaperExecutor:
    """Simulates fills against the live book. No credentials, no orders, no risk."""

    mode = "PAPER"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.orders = 0

    async def open(
        self, *, token_id: str, shares: float, limit_price: float, book: BookSnapshot | None
    ) -> Fill:
        self.orders += 1
        if book is None or not book.asks:
            return Fill(token_id, "BUY", 0.0, 0.0, status="UNFILLED", mode=self.mode,
                        detail="no asks available")

        filled, vwap = book.fill_buy(shares, limit_price)
        if filled <= 0:
            return Fill(token_id, "BUY", 0.0, 0.0, status="UNFILLED", mode=self.mode,
                        detail=f"nothing offered at or below ${limit_price:.3f}")

        price = min(MAX_PRICE, vwap + self.cfg.slippage)
        status = "FILLED" if filled >= shares - 1e-9 else "PARTIAL"
        return Fill(
            token_id, "BUY", filled, price,
            fees=_fee(filled * price, self.cfg),
            order_id=f"paper-{self.orders}",
            status=status,
            mode=self.mode,
            detail=f"vwap ${vwap:.4f} + slippage ${self.cfg.slippage:.4f}",
        )

    async def close(
        self, *, position: Position, shares: float, limit_price: float,
        book: BookSnapshot | None,
    ) -> Fill:
        self.orders += 1
        token_id = position.token_id
        if book is None or not book.bids:
            return Fill(token_id, "SELL", 0.0, 0.0, status="UNFILLED", mode=self.mode,
                        detail="no bids available")

        filled, vwap = book.fill_sell(shares, limit_price)
        if filled <= 0:
            return Fill(token_id, "SELL", 0.0, 0.0, status="UNFILLED", mode=self.mode,
                        detail=f"no bid at or above ${limit_price:.3f}")

        price = max(MIN_PRICE, vwap - self.cfg.slippage)
        status = "FILLED" if filled >= shares - 1e-9 else "PARTIAL"
        return Fill(
            token_id, "SELL", filled, price,
            fees=_fee(filled * price, self.cfg),
            order_id=f"paper-{self.orders}",
            status=status,
            mode=self.mode,
            detail=f"vwap ${vwap:.4f} - slippage ${self.cfg.slippage:.4f}",
        )

    async def cancel_all(self) -> int:
        return 0

    async def available_balance(self) -> float | None:
        return None


# ─────────────────────────────────────────────
# LIVE
# ─────────────────────────────────────────────

class LiveExecutor:
    """Posts real fill-or-kill orders to the Polymarket CLOB."""

    mode = "LIVE"

    def __init__(self, cfg: Config, gateway: ClobGateway):
        if not cfg.live:
            # Belt and braces: the engine already checks, but constructing a
            # live executor without the three gates would be a serious bug.
            raise ExecutionError(
                "refusing to build a live executor: " + cfg.gate.explain()
            )
        self.cfg = cfg
        self.gateway = gateway
        self.orders = 0
        self.rejected = 0
        self.open_order_ids: set[str] = set()

    # ── order plumbing ───────────────────────────────────────

    def _order_type(self) -> Any:
        from py_clob_client.clob_types import OrderType

        return getattr(OrderType, self.cfg.order_type, OrderType.FOK)

    async def _post(self, *, token_id: str, side: str, shares: float, price: float,
                    tick_size: float) -> Fill:
        from py_clob_client.clob_types import OrderArgs

        price = round_to_tick(
            min(MAX_PRICE, max(MIN_PRICE, price)),
            tick_size,
            mode="up" if side == "BUY" else "down",
        )
        shares = round(shares, 2)
        if shares <= 0:
            return Fill(token_id, side, 0.0, 0.0, status="REJECTED", mode=self.mode,
                        detail="zero size")

        args = OrderArgs(token_id=token_id, price=price, size=shares, side=side)
        self.orders += 1
        client = self.gateway.client

        def _create_and_post() -> Any:
            signed = client.create_order(args)
            return client.post_order(signed, self._order_type())

        try:
            response = await self.gateway.call(
                _create_and_post,
                description=f"post {side} {shares}@{price} {token_id[:10]}",
                attempts=2,
            )
        except Exception as exc:  # noqa: BLE001 - a failed order is not fatal
            self.rejected += 1
            log.error("order rejected (%s %s %.2f @ %.3f): %s", side, token_id[:12], shares, price, exc)
            return Fill(token_id, side, 0.0, price, status="REJECTED", mode=self.mode,
                        detail=str(exc)[:400])

        return self._parse_response(response, token_id=token_id, side=side,
                                    requested=shares, limit_price=price)

    def _parse_response(
        self, response: Any, *, token_id: str, side: str, requested: float, limit_price: float
    ) -> Fill:
        """Turn a CLOB post-order response into a `Fill`, defensively.

        The response shape varies with order type and match outcome, so every
        field is treated as optional and the conservative reading wins: if we
        cannot prove a fill happened, we report no fill.
        """
        payload = response if isinstance(response, dict) else {}
        order_id = str(payload.get("orderID") or payload.get("order_id") or "")
        status = str(payload.get("status") or "").lower()
        success = payload.get("success")
        error = str(payload.get("errorMsg") or payload.get("error") or "")

        if success is False or error:
            self.rejected += 1
            return Fill(token_id, side, 0.0, limit_price, order_id=order_id,
                        status="REJECTED", mode=self.mode, detail=error[:400] or "rejected")

        making = _safe_float(payload.get("makingAmount"))
        taking = _safe_float(payload.get("takingAmount"))
        shares = price = 0.0
        if making and taking:
            # For a BUY we pay USDC (making) and receive shares (taking);
            # for a SELL the roles swap.
            shares = taking if side == "BUY" else making
            usdc = making if side == "BUY" else taking
            if shares > 0:
                price = usdc / shares
        if shares <= 0 and status in ("matched", "filled"):
            shares, price = requested, limit_price   # matched with no amounts reported

        if shares <= 0:
            detail = f"status={status or 'unknown'} (no fill)"
            if order_id and status in ("live", "delayed"):
                # A resting FOK should not happen, but if one does, track it so
                # shutdown can cancel it.
                self.open_order_ids.add(order_id)
            return Fill(token_id, side, 0.0, limit_price, order_id=order_id,
                        status="UNFILLED", mode=self.mode, detail=detail)

        fill_status = "FILLED" if shares >= requested - 1e-6 else "PARTIAL"
        return Fill(
            token_id, side, shares, price or limit_price,
            fees=_fee(shares * (price or limit_price), self.cfg),
            order_id=order_id, status=fill_status, mode=self.mode,
            detail=f"status={status or 'matched'}",
            at=utc_now(),
        )

    # ── executor interface ───────────────────────────────────

    async def open(
        self, *, token_id: str, shares: float, limit_price: float, book: BookSnapshot | None
    ) -> Fill:
        tick = book.tick_size if book is not None else 0.01
        return await self._post(token_id=token_id, side="BUY", shares=shares,
                                price=limit_price, tick_size=tick)

    async def close(
        self, *, position: Position, shares: float, limit_price: float,
        book: BookSnapshot | None,
    ) -> Fill:
        tick = book.tick_size if book is not None else 0.01
        return await self._post(token_id=position.token_id, side="SELL", shares=shares,
                                price=limit_price, tick_size=tick)

    async def cancel_all(self) -> int:
        """Cancel every resting order. Called on shutdown and on a kill switch."""
        client = self.gateway.client
        try:
            await self.gateway.call(client.cancel_all, description="cancel_all", attempts=3)
        except Exception as exc:  # noqa: BLE001 - report, do not mask shutdown
            log.error("cancel_all failed: %s", exc)
            raise ExecutionError(f"cancel_all failed: {exc}") from exc
        cancelled = len(self.open_order_ids)
        self.open_order_ids.clear()
        log.info("cancelled all resting orders (%d tracked)", cancelled)
        return cancelled

    async def available_balance(self) -> float | None:
        """USDC collateral available, or None when the query is unsupported."""
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            client = self.gateway.client
            response = await self.gateway.call(
                client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
                description="balance-allowance",
                attempts=2,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to configured bankroll
            log.warning("balance lookup failed: %s", exc)
            return None
        raw = (response or {}).get("balance") if isinstance(response, dict) else None
        value = _safe_float(raw)
        if value is None:
            return None
        # Balances come back in USDC base units (6 decimals).
        return value / 1e6 if value > 1e4 else value


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────

def build_executor(cfg: Config, gateway: ClobGateway) -> Executor:
    """Return the live executor only when all three gates are open."""
    if cfg.live:
        log.critical("LIVE TRADING ENABLED -- real funds are at risk")
        return LiveExecutor(cfg, gateway)
    log.info("paper trading mode: %s", cfg.gate.explain())
    return PaperExecutor(cfg)
