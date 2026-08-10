"""
Fractional Kelly position sizing for binary contracts.

For a contract bought at price `c` (dollars per share, paying $1 on a win) with
true win probability `p`, the net odds are b = (1 - c) / c, and the Kelly stake
as a fraction of bankroll is

    f* = (p * b - (1 - p)) / b = (p - c) / (1 - c)

which is simply the edge divided by the payoff. The bot stakes `kelly_fraction`
of that -- 0.5 by default, i.e. half-Kelly -- then applies the hard caps: the
per-position ceiling, the portfolio exposure budget, and whatever the order book
can actually absorb.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config

#: Polymarket accepts order sizes to two decimal places.
SIZE_DECIMALS = 2


# ─────────────────────────────────────────────
# KELLY
# ─────────────────────────────────────────────

def kelly_fraction_binary(prob: float, price: float) -> float:
    """Full-Kelly stake fraction for a binary contract. Never negative.

    A zero result means the bet has no edge at this price and should be skipped.
    """
    if not 0.0 <= prob <= 1.0:
        raise ValueError(f"prob must be in [0, 1], got {prob}")
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    edge = prob - price
    if edge <= 0:
        return 0.0
    return edge / (1.0 - price)


def fractional_kelly(prob: float, price: float, fraction: float) -> float:
    """`fraction` of the full-Kelly stake (0.5 == half-Kelly)."""
    if fraction <= 0:
        raise ValueError("fraction must be positive")
    return kelly_fraction_binary(prob, price) * fraction


def round_shares(shares: float) -> float:
    """Round down to the exchange's size precision."""
    if shares <= 0:
        return 0.0
    factor = 10 ** SIZE_DECIMALS
    return math.floor(shares * factor) / factor


# ─────────────────────────────────────────────
# SIZING
# ─────────────────────────────────────────────

@dataclass
class PositionSize:
    """The outcome of a sizing request, including why it was capped."""

    shares: float = 0.0
    notional: float = 0.0
    price: float = 0.0
    kelly_full: float = 0.0
    kelly_scaled: float = 0.0
    fraction_of_equity: float = 0.0
    binding_constraint: str = ""
    ok: bool = False
    reason: str = ""

    def describe(self) -> str:
        if not self.ok:
            return f"no size ({self.reason})"
        return (
            f"{self.shares:.2f} sh @ ${self.price:.3f} = ${self.notional:,.2f} "
            f"({self.fraction_of_equity:.2%} of equity, "
            f"kelly {self.kelly_full:.2%}->{self.kelly_scaled:.2%}, "
            f"capped by {self.binding_constraint})"
        )


def size_position(
    *,
    prob: float,
    price: float,
    equity: float,
    cfg: Config,
    available_notional: float,
    exposure_budget: float,
    min_order_size: float = 5.0,
) -> PositionSize:
    """Half-Kelly size, clipped by every applicable cap.

    `available_notional` is the dollar depth resting at or better than `price`;
    `exposure_budget` is the dollars of new exposure the risk manager will still
    allow. Both act as hard ceilings on the Kelly stake.
    """
    result = PositionSize(price=price)

    if equity <= 0:
        result.reason = "equity is zero or negative"
        return result
    if not 0.0 < price < 1.0:
        result.reason = f"price {price} outside (0, 1)"
        return result

    result.kelly_full = kelly_fraction_binary(prob, price)
    if result.kelly_full <= 0:
        result.reason = "no Kelly edge at this price"
        return result
    result.kelly_scaled = result.kelly_full * cfg.kelly_fraction

    # Apply caps in order, remembering which one actually bound.
    caps: list[tuple[str, float]] = [
        ("kelly", result.kelly_scaled * equity),
        ("max_position_pct", cfg.max_position_pct * equity),
        ("exposure_budget", max(0.0, exposure_budget)),
        ("book_depth", max(0.0, available_notional)),
    ]
    constraint, notional = min(caps, key=lambda item: item[1])

    shares = round_shares(notional / price)
    notional = shares * price

    floor_usd = max(cfg.min_position_usd, min_order_size * price)
    if shares <= 0 or notional < floor_usd:
        result.binding_constraint = constraint
        result.reason = (
            f"size ${notional:,.2f} below minimum ${floor_usd:,.2f} "
            f"(bound by {constraint})"
        )
        return result

    result.shares = shares
    result.notional = notional
    result.fraction_of_equity = notional / equity
    result.binding_constraint = constraint
    result.ok = True
    result.reason = "sized"
    return result
