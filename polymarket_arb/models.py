"""
Domain objects shared by execution, storage, and the dashboard.

A `Position` is one binary contract holding: bought at a price, marked against
the live book, and closed either by an early exit or by the contract settling at
expiry. Everything the post-mortem needs -- the model's fair value, the edge and
confidence at entry, the spot and strike -- is captured on the position itself
so the SQLite history is self-describing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

OPEN = "OPEN"
CLOSED = "CLOSED"


def utc_now() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────
# FILLS
# ─────────────────────────────────────────────

@dataclass
class Fill:
    """The result of an execution attempt, real or simulated."""

    token_id: str
    side: str                 # "BUY" or "SELL"
    shares: float
    price: float              # average fill price
    fees: float = 0.0
    order_id: str = ""
    status: str = "FILLED"    # FILLED | PARTIAL | REJECTED | UNFILLED
    mode: str = "PAPER"
    at: datetime = field(default_factory=utc_now)
    detail: str = ""

    @property
    def notional(self) -> float:
        return self.shares * self.price

    @property
    def cost(self) -> float:
        """Cash out of the door for a buy (or cash in, negated, for a sell)."""
        return self.notional + self.fees

    @property
    def filled(self) -> bool:
        return self.shares > 0 and self.status in ("FILLED", "PARTIAL")


# ─────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────

@dataclass
class Position:
    """One open (or historical) contract holding."""

    condition_id: str
    market_label: str
    asset: str
    window_minutes: int
    side: str                       # "UP" or "DOWN"
    token_id: str
    shares: float
    entry_price: float
    entry_fees: float = 0.0
    entry_time: datetime = field(default_factory=utc_now)
    close_time: datetime | None = None      # contract expiry, not our exit

    # -- decision context, kept for post-trade analysis --
    strike: float = 0.0
    spot_at_entry: float = 0.0
    fair_prob: float = 0.0
    market_mid: float = 0.0
    divergence: float = 0.0
    edge: float = 0.0
    confidence: float = 0.0
    kelly_fraction: float = 0.0
    sigma: float = 0.0
    seconds_left_at_entry: float = 0.0

    # -- lifecycle --
    id: int | None = None
    status: str = OPEN
    mode: str = "PAPER"
    entry_order_id: str = ""
    exit_order_id: str = ""
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str = ""
    exit_fees: float = 0.0
    settlement_price: float | None = None   # underlying price at settlement
    realized_pnl: float | None = None

    # ── economics ────────────────────────────────────────────

    @property
    def cost_basis(self) -> float:
        """Cash paid to open, including fees."""
        return self.shares * self.entry_price + self.entry_fees

    def market_value(self, mark: float) -> float:
        return self.shares * mark

    def unrealized_pnl(self, mark: float) -> float:
        return self.market_value(mark) - self.cost_basis

    def unrealized_pct(self, mark: float) -> float:
        basis = self.cost_basis
        return 0.0 if basis <= 0 else self.unrealized_pnl(mark) / basis

    def payoff_if_correct(self) -> float:
        """P&L if the contract settles in our favour ($1 per share)."""
        return self.shares * 1.0 - self.cost_basis

    def close(
        self,
        *,
        exit_price: float,
        reason: str,
        at: datetime | None = None,
        fees: float = 0.0,
        order_id: str = "",
        settlement_price: float | None = None,
    ) -> float:
        """Mark the position closed and return its realized P&L."""
        self.exit_price = exit_price
        self.exit_time = at or utc_now()
        self.exit_reason = reason
        self.exit_fees = fees
        self.exit_order_id = order_id
        self.settlement_price = settlement_price
        self.status = CLOSED
        self.realized_pnl = self.shares * exit_price - self.cost_basis - fees
        return self.realized_pnl

    @property
    def is_open(self) -> bool:
        return self.status == OPEN

    @property
    def won(self) -> bool | None:
        if self.realized_pnl is None:
            return None
        return self.realized_pnl > 0

    def seconds_to_expiry(self, now: datetime | None = None) -> float:
        if self.close_time is None:
            return float("inf")
        return (self.close_time - (now or utc_now())).total_seconds()

    def to_row(self) -> dict[str, Any]:
        """Flat dict for SQLite, with datetimes as ISO-8601 strings."""
        row = asdict(self)
        for key in ("entry_time", "close_time", "exit_time"):
            value = row.get(key)
            row[key] = value.isoformat() if isinstance(value, datetime) else value
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Position":
        data = dict(row)
        data.pop("id", None)
        for key in ("entry_time", "close_time", "exit_time"):
            value = data.get(key)
            if isinstance(value, str) and value:
                try:
                    data[key] = datetime.fromisoformat(value)
                except ValueError:
                    data[key] = None
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        position = cls(**{k: v for k, v in data.items() if k in known and k != "id"})
        position.id = row.get("id")
        return position

    def describe(self) -> str:
        return (
            f"#{self.id or '-'} {self.market_label} {self.side} "
            f"{self.shares:.2f}sh @ ${self.entry_price:.3f}"
        )


# ─────────────────────────────────────────────
# AGGREGATE STATS
# ─────────────────────────────────────────────

@dataclass
class TradeStats:
    """Summary of closed trades, as shown on the dashboard."""

    total: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    best: float = 0.0
    worst: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_profit - self.gross_loss

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return 0.0 if decided == 0 else self.wins / decided

    @property
    def profit_factor(self) -> float:
        if self.gross_loss <= 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def avg_win(self) -> float:
        return 0.0 if self.wins == 0 else self.gross_profit / self.wins

    @property
    def avg_loss(self) -> float:
        return 0.0 if self.losses == 0 else self.gross_loss / self.losses

    @property
    def expectancy(self) -> float:
        """Average P&L per closed trade."""
        return 0.0 if self.total == 0 else self.net_pnl / self.total

    @classmethod
    def from_pnls(cls, pnls: list[float]) -> "TradeStats":
        stats = cls(total=len(pnls))
        for pnl in pnls:
            if pnl > 0:
                stats.wins += 1
                stats.gross_profit += pnl
            elif pnl < 0:
                stats.losses += 1
                stats.gross_loss += -pnl
            else:
                stats.breakeven += 1
        if pnls:
            stats.best = max(pnls)
            stats.worst = min(pnls)
        return stats
