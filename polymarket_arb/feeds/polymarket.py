"""
Polymarket CLOB access: market discovery and order book polling.

`ClobGateway` owns the (synchronous) `py_clob_client.ClobClient` and is the only
place blocking calls are made -- every one is dispatched to a thread, paced by a
shared token bucket, retried with backoff, and guarded by a circuit breaker so a
comprehensive outage does not turn into a request storm. Both this feed and the
live executor go through it.

Discovery prefers the Gamma API, which can filter by end date and so returns
only the short-dated contracts we care about, and falls back to paging the CLOB
`/markets` endpoint when Gamma is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, TypeVar

import httpx

from ..config import Config
from ..markets import (
    CryptoUpDownMarket,
    ParseReport,
    normalize_gamma_market,
    parse_market,
)
from ..orderbook import BookSnapshot
from ..ratelimit import CircuitBreaker, RetryPolicy, TokenBucket, gather_bounded, retry_async

log = logging.getLogger(__name__)

T = TypeVar("T")


class ClobUnavailable(RuntimeError):
    """The CLOB could not be reached, or the circuit breaker is open."""


# ─────────────────────────────────────────────
# GATEWAY
# ─────────────────────────────────────────────

class ClobGateway:
    """Rate-limited, retrying, thread-offloaded access to the CLOB client."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bucket = TokenBucket(
            cfg.clob_requests_per_second, cfg.clob_burst, name="clob"
        )
        self.breaker = CircuitBreaker(threshold=6, reset_after=30.0, name="clob")
        self.retry = RetryPolicy(attempts=4, base_delay=0.4, max_delay=8.0)
        self._client: Any | None = None
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.last_latency_ms = 0.0

    # ── client construction ──────────────────────────────────

    def build_client(self) -> Any:
        """Construct the ClobClient at the highest auth level the env allows.

        Level 0 (host only) is enough to read books and markets, which is all
        paper mode needs. A private key upgrades to level 1 (signing), and API
        credentials -- supplied or derived -- to level 2 (order placement).
        """
        if self._client is not None:
            return self._client

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ClobUnavailable(
                "py-clob-client is not installed; run `pip install py-clob-client`"
            ) from exc

        creds = self.cfg.credentials
        kwargs: dict[str, Any] = {"host": self.cfg.clob_host, "chain_id": self.cfg.chain_id}
        if creds.private_key:
            kwargs["key"] = creds.private_key
        if creds.signature_type is not None:
            kwargs["signature_type"] = creds.signature_type
        if creds.funder:
            kwargs["funder"] = creds.funder

        client = ClobClient(**kwargs)

        if creds.private_key:
            try:
                if creds.has_api_creds:
                    client.set_api_creds(
                        ApiCreds(
                            api_key=creds.api_key,
                            api_secret=creds.api_secret,
                            api_passphrase=creds.api_passphrase,
                        )
                    )
                else:
                    # Deriving is idempotent: it returns the existing key for
                    # this signer rather than minting a second one.
                    client.set_api_creds(client.create_or_derive_api_creds())
                log.info("CLOB client authenticated at level 2 (order placement enabled)")
            except Exception as exc:  # noqa: BLE001 - read-only mode is a valid fallback
                log.warning(
                    "could not establish CLOB API credentials (%s); "
                    "continuing read-only -- live orders will be rejected", exc,
                )
        else:
            log.info("CLOB client running at level 0 (public read-only endpoints)")

        self._client = client
        return client

    @property
    def client(self) -> Any:
        return self.build_client()

    @property
    def can_trade(self) -> bool:
        """True when the client holds both a signer and API credentials."""
        client = self._client
        if client is None:
            return False
        return bool(getattr(client, "signer", None) and getattr(client, "creds", None))

    # ── call plumbing ────────────────────────────────────────

    async def call(
        self,
        fn: Callable[..., T],
        *args: Any,
        description: str = "clob call",
        attempts: int | None = None,
        **kwargs: Any,
    ) -> T:
        """Run a blocking client method off the event loop, paced and retried."""
        if self.breaker.is_open:
            raise ClobUnavailable(
                f"CLOB circuit open, retrying in {self.breaker.seconds_until_reset():.0f}s"
            )
        policy = self.retry if attempts is None else RetryPolicy(
            attempts=attempts, base_delay=self.retry.base_delay, max_delay=self.retry.max_delay
        )

        async def _once() -> T:
            await self.bucket.acquire()
            started = time.monotonic()
            try:
                result = await asyncio.to_thread(fn, *args, **kwargs)
            finally:
                self.last_latency_ms = (time.monotonic() - started) * 1000.0
            self.calls += 1
            return result

        try:
            result = await retry_async(_once, policy=policy, description=description)
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{description}: {exc}"
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return result

    def status(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "circuit_open": self.breaker.is_open,
            "last_error": self.last_error,
            "latency_ms": round(self.last_latency_ms, 1),
            "can_trade": self.can_trade,
        }


# ─────────────────────────────────────────────
# FEED
# ─────────────────────────────────────────────

class PolymarketFeed:
    """Discovers up/down markets and keeps their order books fresh."""

    #: Never page the CLOB market list forever; discovery must stay bounded.
    MAX_CLOB_PAGES = 8

    def __init__(self, cfg: Config, gateway: ClobGateway | None = None):
        self.cfg = cfg
        self.gateway = gateway or ClobGateway(cfg)
        self.books: dict[str, BookSnapshot] = {}
        self.markets: dict[str, CryptoUpDownMarket] = {}
        self.last_discovery: float | None = None
        self.last_discovery_error: str = ""
        self.discovery_source: str = ""
        self.book_errors = 0
        self._http: httpx.AsyncClient | None = None

    # ── lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent": "polymarket-arb/0.1 (+paper-trading)"},
        )
        # Build the (blocking) CLOB client off the loop so a slow key derivation
        # does not stall startup.
        await asyncio.to_thread(self.gateway.build_client)

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── discovery ────────────────────────────────────────────

    async def discover(self) -> list[CryptoUpDownMarket]:
        """Refresh the tradeable universe. Never raises; returns [] on failure."""
        try:
            markets = await self._discover_gamma()
            self.discovery_source = "gamma"
        except Exception as gamma_exc:  # noqa: BLE001 - fall through to the CLOB
            log.warning("Gamma discovery failed (%s); falling back to CLOB /markets", gamma_exc)
            try:
                markets = await self._discover_clob()
                self.discovery_source = "clob"
            except Exception as clob_exc:  # noqa: BLE001 - discovery is best effort
                self.last_discovery_error = f"{type(clob_exc).__name__}: {clob_exc}"
                log.error("market discovery failed entirely: %s", self.last_discovery_error)
                return []

        self.last_discovery = time.monotonic()
        self.last_discovery_error = ""

        # Keep strikes already resolved for markets we have seen before.
        for market in markets:
            existing = self.markets.get(market.condition_id)
            if existing is not None and existing.strike is not None and market.strike is None:
                market.strike = existing.strike
                market.strike_source = existing.strike_source
        self.markets = {m.condition_id: m for m in markets}
        self._prune_books()
        return markets

    async def _discover_gamma(self) -> list[CryptoUpDownMarket]:
        if self._http is None:
            raise ClobUnavailable("feed not started")
        now = datetime.now(UTC)
        # Only markets expiring inside our tradeable horizon (plus a margin so
        # the next window is already known before the current one settles).
        params = {
            "closed": "false",
            "active": "true",
            "limit": "200",
            "order": "endDate",
            "ascending": "true",
            "end_date_min": now.isoformat().replace("+00:00", "Z"),
            "end_date_max": (
                now + timedelta(seconds=self.cfg.max_seconds_to_expiry + 900)
            ).isoformat().replace("+00:00", "Z"),
        }

        async def _fetch() -> list[dict[str, Any]]:
            assert self._http is not None
            response = await self._http.get(f"{self.cfg.gamma_host}/markets", params=params)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                payload = payload.get("data") or payload.get("markets") or []
            return payload if isinstance(payload, list) else []

        payloads = await retry_async(
            _fetch,
            policy=RetryPolicy(attempts=3, base_delay=0.5, max_delay=4.0),
            description="gamma /markets",
        )
        report = ParseReport()
        markets = []
        for raw in payloads:
            if not isinstance(raw, dict):
                continue
            parsed = parse_market(
                normalize_gamma_market(raw),
                assets=self.cfg.assets,
                windows=self.cfg.windows,
                report=report,
            )
            if parsed is not None:
                markets.append(parsed)
        log.info("gamma discovery: scanned %d markets, %s", len(payloads), report.summary())
        return markets

    async def _discover_clob(self) -> list[CryptoUpDownMarket]:
        client = self.gateway.client
        report = ParseReport()
        markets: list[CryptoUpDownMarket] = []
        cursor = ""
        for _ in range(self.MAX_CLOB_PAGES):
            page = await self.gateway.call(
                client.get_markets, cursor, description="clob /markets"
            )
            data = page.get("data") if isinstance(page, dict) else None
            for raw in data or []:
                parsed = parse_market(
                    raw, assets=self.cfg.assets, windows=self.cfg.windows, report=report
                )
                if parsed is not None:
                    markets.append(parsed)
            cursor = (page or {}).get("next_cursor") or ""
            if not cursor or cursor == "LTE=":  # LTE= is the end-of-list cursor
                break
        log.info("clob discovery: %s", report.summary())
        return markets

    def _prune_books(self) -> None:
        """Drop cached books for tokens that are no longer in the universe."""
        live_tokens = {
            token
            for market in self.markets.values()
            for token in (market.up_token_id, market.down_token_id)
        }
        for token in list(self.books):
            if token not in live_tokens:
                self.books.pop(token, None)

    # ── strikes ──────────────────────────────────────────────

    async def resolve_strikes(self, resolver: Callable[[CryptoUpDownMarket], Any]) -> int:
        """Fill in missing strikes using an async `resolver(market) -> float|None`."""
        pending = [m for m in self.markets.values() if m.strike is None]
        if not pending:
            return 0
        results = await gather_bounded(
            (resolver(m) for m in pending), self.cfg.max_concurrent_book_fetches
        )
        resolved = 0
        for market, value in zip(pending, results):
            if isinstance(value, BaseException) or value is None:
                continue
            market.strike = float(value)
            market.strike_source = "feed"
            resolved += 1
        if resolved:
            log.info("resolved strikes for %d market(s)", resolved)
        return resolved

    # ── order books ──────────────────────────────────────────

    async def fetch_book(self, token_id: str, *, tick_size: float = 0.01) -> BookSnapshot | None:
        """Fetch and cache one order book. Returns None on failure."""
        client = self.gateway.client
        try:
            raw = await self.gateway.call(
                client.get_order_book, token_id, description=f"book {token_id[:10]}", attempts=2
            )
        except Exception as exc:  # noqa: BLE001 - a single stale book is survivable
            self.book_errors += 1
            log.debug("book fetch failed for %s: %s", token_id[:12], exc)
            return None
        snapshot = BookSnapshot.from_clob(token_id, raw, tick_size=tick_size)
        self.books[token_id] = snapshot
        return snapshot

    async def refresh_books(self, markets: Iterable[CryptoUpDownMarket]) -> int:
        """Refresh both sides of every supplied market, bounded in concurrency."""
        tasks = []
        for market in markets:
            for token in (market.up_token_id, market.down_token_id):
                tasks.append(self.fetch_book(token, tick_size=market.tick_size))
        if not tasks:
            return 0
        results = await gather_bounded(tasks, self.cfg.max_concurrent_book_fetches)
        return sum(1 for r in results if isinstance(r, BookSnapshot))

    def book(self, token_id: str) -> BookSnapshot | None:
        return self.books.get(token_id)

    def books_for(self, market: CryptoUpDownMarket) -> dict[str, BookSnapshot | None]:
        return {
            market.up_token_id: self.books.get(market.up_token_id),
            market.down_token_id: self.books.get(market.down_token_id),
        }

    def oldest_book_age(self) -> float:
        if not self.books:
            return float("inf")
        now = time.monotonic()
        return max(book.age(now) for book in self.books.values())

    def is_healthy(self) -> bool:
        if self.gateway.breaker.is_open or not self.books:
            return False
        now = time.monotonic()
        fresh = sum(1 for b in self.books.values() if b.age(now) <= self.cfg.max_clob_staleness)
        return fresh > 0

    def status(self) -> dict[str, object]:
        return {
            "markets": len(self.markets),
            "books": len(self.books),
            "book_errors": self.book_errors,
            "source": self.discovery_source,
            "last_error": self.last_discovery_error,
            "oldest_book_age": round(min(self.oldest_book_age(), 9999.0), 2),
            **{f"clob_{k}": v for k, v in self.gateway.status().items()},
        }
