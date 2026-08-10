"""
Order book snapshots and the depth arithmetic used for sizing and fills.

Kept free of I/O so the fill maths can be unit tested directly: the CLOB feed
builds `BookSnapshot`s, the pricing model reads them, and the paper executor
fills against them with exactly the same walk-the-book routine the live sizing
logic uses.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ─────────────────────────────────────────────
# LEVELS
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Level:
    """One price level. `size` is in shares (contracts), not dollars."""

    price: float
    size: float

    @property
    def notional(self) -> float:
        return self.price * self.size


def _coerce_levels(raw: Any) -> list[Level]:
    """Accept dicts, objects with .price/.size, or (price, size) pairs."""
    levels: list[Level] = []
    for item in raw or []:
        price = size = None
        if isinstance(item, dict):
            price, size = item.get("price"), item.get("size")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            price, size = item[0], item[1]
        else:
            price, size = getattr(item, "price", None), getattr(item, "size", None)
        try:
            p, s = float(price), float(size)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if s > 0 and 0.0 < p < 1.0:
            levels.append(Level(p, s))
    return levels


# ─────────────────────────────────────────────
# SNAPSHOT
# ─────────────────────────────────────────────

@dataclass
class BookSnapshot:
    """Top-of-book plus depth for a single CLOB token.

    `bids` are sorted best (highest) first and `asks` best (lowest) first,
    regardless of the ordering the API happened to return.
    """

    token_id: str
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    #: Exchange-supplied timestamp in epoch seconds, when available.
    exchange_ts: float | None = None
    #: Local monotonic clock reading when the snapshot was received.
    received_at: float = field(default_factory=time.monotonic)
    tick_size: float = 0.01

    # ── construction ────────────────────────────────────────

    @classmethod
    def from_clob(
        cls, token_id: str, payload: Any, *, tick_size: float = 0.01
    ) -> "BookSnapshot":
        """Build from a CLOB `/book` response or an `OrderBookSummary` object."""
        if isinstance(payload, dict):
            raw_bids, raw_asks = payload.get("bids"), payload.get("asks")
            ts_raw = payload.get("timestamp")
            tick_raw = payload.get("tick_size")
        else:
            raw_bids = getattr(payload, "bids", None)
            raw_asks = getattr(payload, "asks", None)
            ts_raw = getattr(payload, "timestamp", None)
            tick_raw = getattr(payload, "tick_size", None)

        exchange_ts: float | None = None
        if ts_raw not in (None, ""):
            try:
                exchange_ts = float(ts_raw)
                if exchange_ts > 1e11:  # milliseconds
                    exchange_ts /= 1000.0
            except (TypeError, ValueError):
                exchange_ts = None

        try:
            tick = float(tick_raw) if tick_raw not in (None, "") else tick_size
        except (TypeError, ValueError):
            tick = tick_size

        bids = sorted(_coerce_levels(raw_bids), key=lambda lvl: lvl.price, reverse=True)
        asks = sorted(_coerce_levels(raw_asks), key=lambda lvl: lvl.price)
        return cls(
            token_id=str(token_id),
            bids=bids,
            asks=asks,
            exchange_ts=exchange_ts,
            tick_size=tick or tick_size,
        )

    # ── top of book ─────────────────────────────────────────

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None and ask is None:
            return None
        if bid is None:
            return ask
        if ask is None:
            return bid
        return (bid + ask) / 2.0

    @property
    def spread(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return max(0.0, ask - bid)

    @property
    def is_two_sided(self) -> bool:
        return bool(self.bids and self.asks)

    def age(self, now: float | None = None) -> float:
        """Seconds since this snapshot was received."""
        return (now if now is not None else time.monotonic()) - self.received_at

    # ── depth arithmetic ────────────────────────────────────

    def _walk(self, levels: Sequence[Level], shares: float) -> tuple[float, float] | None:
        """Average price and shares obtainable by consuming `shares` from `levels`.

        Returns None when the book cannot fill the whole quantity.
        """
        if shares <= 0:
            return None
        remaining = shares
        cost = 0.0
        for lvl in levels:
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            remaining -= take
            if remaining <= 1e-9:
                return cost / shares, shares
        return None

    def buy_vwap(self, shares: float) -> float | None:
        """Average fill price to buy `shares`, or None if depth is insufficient."""
        result = self._walk(self.asks, shares)
        return None if result is None else result[0]

    def sell_vwap(self, shares: float) -> float | None:
        """Average fill price to sell `shares`, or None if depth is insufficient."""
        result = self._walk(self.bids, shares)
        return None if result is None else result[0]

    def fill_buy(self, shares: float, limit_price: float | None = None) -> tuple[float, float]:
        """Partially fill a buy against the asks.

        Returns `(filled_shares, average_price)`; `(0.0, 0.0)` when nothing is
        takeable at or below `limit_price`.
        """
        return self._partial(self.asks, shares, limit_price, is_buy=True)

    def fill_sell(self, shares: float, limit_price: float | None = None) -> tuple[float, float]:
        """Partially fill a sell against the bids. See `fill_buy`."""
        return self._partial(self.bids, shares, limit_price, is_buy=False)

    def _partial(
        self,
        levels: Sequence[Level],
        shares: float,
        limit_price: float | None,
        *,
        is_buy: bool,
    ) -> tuple[float, float]:
        remaining = max(0.0, shares)
        filled = 0.0
        cost = 0.0
        for lvl in levels:
            if limit_price is not None:
                if is_buy and lvl.price > limit_price + 1e-12:
                    break
                if not is_buy and lvl.price < limit_price - 1e-12:
                    break
            if remaining <= 1e-9:
                break
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            filled += take
            remaining -= take
        if filled <= 0:
            return 0.0, 0.0
        return filled, cost / filled

    def shares_available(self, limit_price: float, *, side: str = "BUY") -> float:
        """Shares takeable at or better than `limit_price`."""
        if side.upper() == "BUY":
            return sum(l.size for l in self.asks if l.price <= limit_price + 1e-12)
        return sum(l.size for l in self.bids if l.price >= limit_price - 1e-12)

    def notional_available(self, limit_price: float, *, side: str = "BUY") -> float:
        """Dollar notional takeable at or better than `limit_price`."""
        if side.upper() == "BUY":
            return sum(l.notional for l in self.asks if l.price <= limit_price + 1e-12)
        return sum(l.notional for l in self.bids if l.price >= limit_price - 1e-12)

    def total_depth_notional(self) -> float:
        return sum(l.notional for l in self.bids) + sum(l.notional for l in self.asks)


def round_to_tick(price: float, tick: float, *, mode: str = "nearest") -> float:
    """Snap `price` to the market's tick grid, clamped inside (0, 1)."""
    if tick <= 0:
        return price
    steps = price / tick
    if mode == "up":
        snapped = -(-steps // 1) * tick
    elif mode == "down":
        snapped = (steps // 1) * tick
    else:
        snapped = round(steps) * tick
    # Prices must stay strictly inside the unit interval for the CLOB to accept them.
    snapped = min(max(snapped, tick), 1.0 - tick)
    # Kill binary float dust like 0.30000000000000004.
    decimals = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[-1]))
    return round(snapped, decimals)


def merge_books(books: Iterable[BookSnapshot]) -> float:
    """Total notional depth across several books, used for liquidity scoring."""
    return sum(b.total_depth_notional() for b in books)
