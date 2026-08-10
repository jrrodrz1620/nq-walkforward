"""Shared fixtures and builders for the bot's tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polymarket_arb.config import Config
from polymarket_arb.markets import CryptoUpDownMarket
from polymarket_arb.orderbook import BookSnapshot

#: Annualised 50% vol expressed per square-root-second, the units the model uses.
SIGMA_50PCT = 0.5 / (365 * 24 * 3600) ** 0.5


@pytest.fixture
def cfg(tmp_path) -> Config:
    """A validated paper-mode config writing to a temporary database."""
    return Config(
        starting_bankroll=1_000.0,
        db_path=tmp_path / "test.sqlite3",
        log_path=None,
        dashboard_enabled=False,
        min_signal_ticks=1,
    ).validate()


def make_market(
    *,
    asset: str = "BTC",
    window: int = 5,
    seconds_left: float = 150.0,
    strike: float | None = 100_000.0,
    now: datetime | None = None,
) -> CryptoUpDownMarket:
    """A BTC/ETH up-down contract positioned relative to `now`."""
    now = now or datetime.now(UTC)
    close = now + timedelta(seconds=seconds_left)
    market = CryptoUpDownMarket(
        condition_id=f"0xcond{asset}{window}",
        question=f"{asset} Up or Down - {window} minute",
        slug=f"{asset.lower()}-up-or-down-{window}m",
        asset=asset,
        window_minutes=window,
        close_time=close,
        open_time=close - timedelta(minutes=window),
        up_token_id=f"tok-{asset}-{window}-up",
        down_token_id=f"tok-{asset}-{window}-down",
        tick_size=0.01,
        min_order_size=5.0,
    )
    market.strike = strike
    market.strike_source = "test"
    return market


def make_book(
    token_id: str,
    *,
    bid: float,
    ask: float,
    size: float = 500.0,
    levels: int = 3,
) -> BookSnapshot:
    """A tidy two-sided book stepping away from the touch in one-tick levels."""
    return BookSnapshot(
        token_id=token_id,
        bids=[
            _level(max(0.01, bid - 0.01 * i), size) for i in range(levels)
        ],
        asks=[
            _level(min(0.99, ask + 0.01 * i), size) for i in range(levels)
        ],
        tick_size=0.01,
    )


def _level(price: float, size: float):
    from polymarket_arb.orderbook import Level

    return Level(round(price, 4), size)
