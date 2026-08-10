"""
Rate limiting and retry primitives shared by every outbound call.

Everything that leaves the process -- CLOB REST, Binance REST, Telegram -- goes
through a `TokenBucket` for pacing and `retry_async` for transient failures, so
the failure behaviour is identical no matter which subsystem is talking.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


# ─────────────────────────────────────────────
# TOKEN BUCKET
# ─────────────────────────────────────────────

class TokenBucket:
    """Async token bucket.

    `rate` tokens are added per second up to `capacity`. `acquire()` waits until
    a token is available, so callers are paced rather than rejected. Safe to
    share between tasks.
    """

    def __init__(self, rate: float, capacity: float | None = None, *, name: str = ""):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate))
        self.name = name
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds spent waiting."""
        if tokens > self.capacity:
            raise ValueError(f"cannot acquire {tokens} from bucket of {self.capacity}")
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
            waited += delay
            await asyncio.sleep(delay)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking variant; returns False instead of waiting."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


# ─────────────────────────────────────────────
# RETRY
# ─────────────────────────────────────────────

@dataclass
class RetryPolicy:
    """Exponential backoff with full jitter."""

    attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    factor: float = 2.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Delay before retry number `attempt` (1-based)."""
        raw = min(self.max_delay, self.base_delay * (self.factor ** max(0, attempt - 1)))
        if not self.jitter:
            return raw
        return random.uniform(0.0, raw)


class RetryExhausted(RuntimeError):
    """Raised when every retry attempt failed. Carries the final exception."""

    def __init__(self, description: str, attempts: int, last: BaseException):
        super().__init__(f"{description} failed after {attempts} attempt(s): {last!r}")
        self.description = description
        self.attempts = attempts
        self.last = last


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    description: str = "call",
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (asyncio.CancelledError,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Await `fn()`, retrying transient failures with backoff.

    `give_up_on` is checked first so cancellation (and any caller-supplied
    permanent errors) propagate immediately instead of being retried.
    """
    policy = policy or RetryPolicy()
    last: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return await fn()
        except give_up_on:
            raise
        except retry_on as exc:  # noqa: PERF203 - retry loop is the point
            last = exc
            if attempt >= policy.attempts:
                break
            delay = policy.delay_for(attempt)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            else:
                log.warning(
                    "%s failed (attempt %d/%d): %s -- retrying in %.2fs",
                    description, attempt, policy.attempts, exc, delay,
                )
            await asyncio.sleep(delay)
    assert last is not None
    raise RetryExhausted(description, policy.attempts, last) from last


# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────

@dataclass
class CircuitBreaker:
    """Trips open after `threshold` consecutive failures, recovers after `reset_after`.

    Used to stop hammering an endpoint that is comprehensively down, without
    taking the whole bot offline.
    """

    threshold: int = 5
    reset_after: float = 60.0
    name: str = "circuit"
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after:
            # Half-open: let the next call through and judge by its result.
            self._opened_at = None
            self._failures = 0
            log.info("circuit %s half-open, allowing a probe call", self.name)
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            log.error(
                "circuit %s opened after %d consecutive failures; pausing %.0fs",
                self.name, self._failures, self.reset_after,
            )

    def seconds_until_reset(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_after - (time.monotonic() - self._opened_at))


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

async def gather_bounded(
    coros: Iterable[Awaitable[T]], limit: int
) -> list[T | BaseException]:
    """`asyncio.gather(..., return_exceptions=True)` with a concurrency cap."""
    sem = asyncio.Semaphore(limit)

    async def _run(c: Awaitable[T]) -> T:
        async with sem:
            return await c

    return await asyncio.gather(*(_run(c) for c in coros), return_exceptions=True)
