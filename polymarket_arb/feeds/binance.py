"""
Real-time Binance spot feed.

Subscribes to the combined stream at wss://stream.binance.com:9443 for both
`@bookTicker` (best bid/ask, the reference price) and `@trade` (carries an
exchange-side event time, which is what lets us measure our own latency). The
connection self-heals: a stalled socket is torn down by a watchdog, and
reconnects back off exponentially with jitter.

The feed also keeps a short price history. That history is what resolves the
*strike* of an up/down contract -- the underlying's price at the window's open
-- with a REST kline lookup as the fallback for windows that opened before the
bot started.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ..config import Config
from ..pricing import RealizedVol
from ..ratelimit import RetryPolicy, TokenBucket

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# QUOTE
# ─────────────────────────────────────────────

@dataclass
class Quote:
    """Best bid/offer for one symbol."""

    symbol: str
    bid: float
    ask: float
    received_at: float = field(default_factory=time.monotonic)
    wall_time: float = field(default_factory=time.time)
    #: Exchange event time in epoch seconds, when the stream provides one.
    exchange_ts: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.ask - self.bid)

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.received_at


# ─────────────────────────────────────────────
# FEED
# ─────────────────────────────────────────────

class BinanceFeed:
    """Maintains live mid prices and realized volatility per configured asset."""

    #: Force a reconnect if the socket goes quiet for this long.
    STALL_TIMEOUT = 20.0
    #: Binance drops connections after 24h; reconnect proactively before then.
    MAX_CONNECTION_SECONDS = 20 * 3600.0

    def __init__(self, cfg: Config, *, history_seconds: float = 1800.0):
        self.cfg = cfg
        self.symbols: dict[str, str] = cfg.symbols          # asset -> binance symbol
        self._by_symbol: dict[str, str] = {v: k for k, v in self.symbols.items()}

        self.quotes: dict[str, Quote] = {}
        self.vol: dict[str, RealizedVol] = {
            asset: RealizedVol(
                halflife_samples=cfg.vol_halflife,
                floor=cfg.min_vol_per_sqrt_sec,
                ceiling=cfg.max_vol_per_sqrt_sec,
            )
            for asset in self.symbols
        }
        self.history: dict[str, deque[tuple[float, float]]] = {
            asset: deque(maxlen=int(history_seconds * 4)) for asset in self.symbols
        }
        self.latency_ms: dict[str, float] = {}

        self.connected = False
        self.connects = 0
        self.reconnects = 0
        self.messages = 0
        self.last_error: str = ""
        self.connected_since: float | None = None

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._rest_bucket = TokenBucket(cfg.binance_rest_per_second, name="binance-rest")
        self._retry = RetryPolicy(attempts=6, base_delay=1.0, max_delay=60.0)
        self._client: httpx.AsyncClient | None = None
        self._history_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────

    @property
    def stream_url(self) -> str:
        streams = []
        for symbol in self.symbols.values():
            streams.append(f"{symbol}@bookTicker")
            streams.append(f"{symbol}@trade")
        return f"{self.cfg.binance_ws_host}/stream?streams=" + "/".join(streams)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), http2=False)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="binance-feed")
        log.info("Binance feed starting: %s", ", ".join(sorted(self.symbols.values())))

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.connected = False
        log.info("Binance feed stopped")

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        """Wait until every configured asset has at least one quote."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(asset in self.quotes for asset in self.symbols):
                return True
            if self._stop.is_set():
                return False
            await asyncio.sleep(0.2)
        return all(asset in self.quotes for asset in self.symbols)

    # ── connection loop ──────────────────────────────────────

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._consume()
                attempt = 0  # a clean return means a deliberate cycle, not a failure
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                attempt += 1
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                delay = self._retry.delay_for(attempt)
                log.warning(
                    "Binance stream dropped (%s); reconnecting in %.1fs (attempt %d)",
                    self.last_error, delay, attempt,
                )
                self.reconnects += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return  # stop requested while backing off
                except asyncio.TimeoutError:
                    continue

    async def _consume(self) -> None:
        url = self.stream_url
        log.debug("connecting to %s", url)
        async with ws_connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            open_timeout=15,
            max_queue=1024,
        ) as ws:
            self.connected = True
            self.connects += 1
            self.connected_since = time.monotonic()
            self.last_error = ""
            log.info("Binance stream connected (%d streams)", len(self.symbols) * 2)
            try:
                while not self._stop.is_set():
                    if time.monotonic() - (self.connected_since or 0) > self.MAX_CONNECTION_SECONDS:
                        log.info("cycling Binance connection before the 24h server limit")
                        return
                    try:
                        async with asyncio.timeout(self.STALL_TIMEOUT):
                            raw = await ws.recv()
                    except asyncio.TimeoutError as exc:
                        raise ConnectionError(
                            f"no data for {self.STALL_TIMEOUT:.0f}s"
                        ) from exc
                    self._handle(raw)
            except ConnectionClosed:
                raise
            finally:
                self.connected = False

    def _handle(self, raw: str | bytes) -> None:
        """Parse one combined-stream frame. Never raises on bad input."""
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("unparseable frame from Binance: %r", raw[:200])
            return
        if not isinstance(envelope, dict):
            return
        data = envelope.get("data", envelope)
        if not isinstance(data, dict):
            return

        self.messages += 1
        symbol = str(data.get("s") or "").lower()
        asset = self._by_symbol.get(symbol)
        if asset is None:
            return

        now_mono = time.monotonic()
        event = data.get("e")

        if event == "trade":
            # Trade tick: gives us the exchange clock, so we can report latency.
            event_ms = data.get("E") or data.get("T")
            if event_ms:
                try:
                    self.latency_ms[asset] = max(
                        0.0, time.time() * 1000.0 - float(event_ms)
                    )
                except (TypeError, ValueError):
                    pass
            return

        # bookTicker: b/B best bid, a/A best ask.
        try:
            bid = float(data["b"])
            ask = float(data["a"])
        except (KeyError, TypeError, ValueError):
            return
        if bid <= 0 or ask <= 0 or ask < bid:
            return

        quote = Quote(symbol=symbol, bid=bid, ask=ask, received_at=now_mono)
        self.quotes[asset] = quote
        mid = quote.mid
        self.vol[asset].update(now_mono, mid)
        self.history[asset].append((quote.wall_time, mid))

    # ── accessors ────────────────────────────────────────────

    def price(self, asset: str) -> float | None:
        quote = self.quotes.get(asset)
        return None if quote is None else quote.mid

    def quote(self, asset: str) -> Quote | None:
        return self.quotes.get(asset)

    def age(self, asset: str, now: float | None = None) -> float:
        quote = self.quotes.get(asset)
        if quote is None:
            return float("inf")
        return quote.age(now)

    def sigma(self, asset: str) -> float:
        estimator = self.vol.get(asset)
        return self.cfg.min_vol_per_sqrt_sec if estimator is None else estimator.sigma_per_sqrt_sec

    def vol_progress(self, asset: str) -> float:
        estimator = self.vol.get(asset)
        return 0.0 if estimator is None else estimator.warmup_progress()

    def is_healthy(self, asset: str | None = None) -> bool:
        """Connected, and quoting within the configured staleness budget."""
        assets = [asset] if asset else list(self.symbols)
        if not self.connected:
            return False
        return all(self.age(a) <= self.cfg.max_binance_staleness for a in assets)

    def status(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "connects": self.connects,
            "reconnects": self.reconnects,
            "messages": self.messages,
            "last_error": self.last_error,
            "prices": {a: self.price(a) for a in self.symbols},
            "ages": {a: round(self.age(a), 2) for a in self.symbols},
            "latency_ms": {a: round(v, 1) for a, v in self.latency_ms.items()},
            "vol_annualized": {
                a: round(self.vol[a].annualized, 4) for a in self.symbols if a in self.vol
            },
        }

    # ── historical prices ────────────────────────────────────

    def price_at(self, asset: str, when: datetime, *, tolerance: float = 5.0) -> float | None:
        """Nearest locally observed price to `when`, if we were watching then."""
        target = when.timestamp()
        series = self.history.get(asset)
        if not series:
            return None
        delta, price = min(((abs(ts - target), px) for ts, px in series), key=lambda p: p[0])
        return price if delta <= tolerance else None

    async def window_open_price(self, asset: str, open_time: datetime) -> float | None:
        """The underlying's price at the open of a contract window (the strike).

        Prefers our own observation of the tape; falls back to Binance's 1-minute
        kline open for windows that started before the bot did.
        """
        local = self.price_at(asset, open_time, tolerance=3.0)
        if local is not None:
            return local
        return await self.kline_price(asset, open_time, field="open")

    async def settlement_price(self, asset: str, close_time: datetime) -> float | None:
        """The underlying's price at contract close, used to settle positions."""
        local = self.price_at(asset, close_time, tolerance=3.0)
        if local is not None:
            return local
        return await self.kline_price(asset, close_time, field="close")

    async def kline_price(
        self, asset: str, when: datetime, *, field: str = "open"
    ) -> float | None:
        """Fetch a 1-minute kline covering `when` and return its open/close.

        Returns None rather than raising: a missing strike simply makes the
        market untradeable until it can be resolved.
        """
        symbol = self.symbols.get(asset)
        if symbol is None or self._client is None:
            return None
        index = {"open": 1, "high": 2, "low": 3, "close": 4}.get(field)
        if index is None:
            raise ValueError(f"unknown kline field {field!r}")

        minute_start = when.replace(second=0, microsecond=0)
        start_ms = int(minute_start.timestamp() * 1000)
        params = {
            "symbol": symbol.upper(),
            "interval": "1m",
            "startTime": start_ms,
            "limit": 1,
        }
        url = f"{self.cfg.binance_rest_host}/api/v3/klines"

        async def _fetch() -> float | None:
            await self._rest_bucket.acquire()
            assert self._client is not None
            response = await self._client.get(url, params=params)
            if response.status_code == 429:
                raise ConnectionError("binance rate limit (429)")
            response.raise_for_status()
            payload = response.json()
            if not payload:
                return None
            return float(payload[0][index])

        try:
            from ..ratelimit import retry_async  # local import avoids a cycle at import time

            return await retry_async(
                _fetch,
                policy=RetryPolicy(attempts=3, base_delay=0.5, max_delay=5.0),
                description=f"binance kline {symbol} {minute_start:%H:%M}",
            )
        except Exception as exc:  # noqa: BLE001 - a missing kline is not fatal
            log.warning("kline lookup failed for %s at %s: %s", asset, minute_start, exc)
            return None


def utc_now() -> datetime:
    return datetime.now(UTC)
