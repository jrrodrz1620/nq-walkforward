"""
Pricing / tick / margin conversion module  (validation gate 3).

Turns raw price changes into points, ticks, and dollars for a given contract,
and answers the margin question the router needs before it sends anything:
*"can the account safely afford the initial margin for this order?"*
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import ContractSpec


@dataclass(frozen=True)
class MarginCheck:
    ok: bool
    required_margin: float
    available_equity: float
    max_allowed: float          # available_equity * max_margin_utilization
    utilization: float          # required / available (inf if no equity)
    reason: str | None = None


class PricingEngine:
    """All point/tick/margin math for one :class:`ContractSpec`."""

    def __init__(self, spec: ContractSpec):
        self.spec = spec

    # ── raw price <-> points / ticks ────────────────────────────────
    def to_points(self, price_change: float) -> float:
        """A price change for index/most futures *is* already in points."""
        return float(price_change)

    def to_ticks(self, price_change: float) -> float:
        """Number of ticks in a raw price change (may be fractional)."""
        return float(price_change) / self.spec.tick_size

    def ticks_to_points(self, ticks: float) -> float:
        return float(ticks) * self.spec.tick_size

    def round_to_tick(self, price: float) -> float:
        """Snap an arbitrary price to the nearest valid tick."""
        ticks = round(float(price) / self.spec.tick_size)
        # Re-multiply in tick units to avoid float drift (e.g. 0.1 + 0.2).
        return round(ticks * self.spec.tick_size, 10)

    def is_on_tick(self, price: float, tol: float = 1e-9) -> bool:
        ticks = float(price) / self.spec.tick_size
        return abs(ticks - round(ticks)) <= tol

    # ── dollars ─────────────────────────────────────────────────────
    def dollar_value(self, price_change: float, quantity: int = 1) -> float:
        """USD PnL for a point move of ``price_change`` over ``quantity`` lots."""
        return float(price_change) * self.spec.multiplier * quantity

    def pnl(self, entry: float, exit_price: float, quantity: int,
            direction: int) -> float:
        """Signed USD PnL. ``direction`` is +1 long / -1 short."""
        return (exit_price - entry) * direction * quantity * self.spec.multiplier

    # ── margin ──────────────────────────────────────────────────────
    def required_margin(self, quantity: int) -> float:
        return self.spec.initial_margin * abs(quantity)

    def check_margin(self, quantity: int, available_equity: float,
                     max_utilization: float) -> MarginCheck:
        """Verify initial margin against equity under a safe utilization cap.

        An order is rejected when its initial margin would consume more than
        ``max_utilization`` of available equity (or when equity is non-positive).
        """
        required = self.required_margin(quantity)
        max_allowed = available_equity * max_utilization
        if available_equity <= 0:
            utilization = math.inf
        else:
            utilization = required / available_equity
        ok = available_equity > 0 and required <= max_allowed
        reason = None
        if not ok:
            reason = (
                f"initial margin ${required:,.2f} for {abs(quantity)}x "
                f"{self.spec.symbol} exceeds safe limit ${max_allowed:,.2f} "
                f"({max_utilization:.0%} of ${available_equity:,.2f} equity)"
            )
        return MarginCheck(
            ok=ok,
            required_margin=required,
            available_equity=available_equity,
            max_allowed=max_allowed,
            utilization=utilization,
            reason=reason,
        )
