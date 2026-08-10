"""Token bucket pacing, retry backoff, and the circuit breaker."""
from __future__ import annotations

import asyncio

import pytest

from polymarket_arb.ratelimit import (
    CircuitBreaker,
    RetryExhausted,
    RetryPolicy,
    TokenBucket,
    gather_bounded,
    retry_async,
)


# ─────────────────────────────────────────────
# TOKEN BUCKET
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bucket_allows_a_burst_then_paces():
    bucket = TokenBucket(rate=50.0, capacity=3.0)
    for _ in range(3):
        assert await bucket.acquire() == 0.0        # burst capacity, no waiting
    assert await bucket.acquire() > 0.0             # refill wait


@pytest.mark.asyncio
async def test_bucket_refills_over_time():
    bucket = TokenBucket(rate=100.0, capacity=1.0)
    await bucket.acquire()
    await asyncio.sleep(0.05)
    assert await bucket.acquire() == 0.0


def test_try_acquire_never_blocks():
    bucket = TokenBucket(rate=1.0, capacity=1.0)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


@pytest.mark.asyncio
async def test_requesting_more_than_capacity_is_an_error():
    bucket = TokenBucket(rate=1.0, capacity=2.0)
    with pytest.raises(ValueError):
        await bucket.acquire(3.0)


def test_rate_must_be_positive():
    with pytest.raises(ValueError):
        TokenBucket(rate=0.0)


# ─────────────────────────────────────────────
# RETRY
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_returns_the_first_success():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("boom")
        return "ok"

    result = await retry_async(
        flaky, policy=RetryPolicy(attempts=5, base_delay=0.001, jitter=False)
    )
    assert result == "ok" and calls == 3


@pytest.mark.asyncio
async def test_retry_gives_up_and_reports_the_last_error():
    async def always_fails():
        raise ValueError("nope")

    with pytest.raises(RetryExhausted) as exc:
        await retry_async(
            always_fails,
            policy=RetryPolicy(attempts=3, base_delay=0.001, jitter=False),
            description="thing",
        )
    assert exc.value.attempts == 3
    assert isinstance(exc.value.last, ValueError)
    assert "thing" in str(exc.value)


@pytest.mark.asyncio
async def test_cancellation_is_never_retried():
    calls = 0

    async def cancelled():
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(cancelled, policy=RetryPolicy(attempts=4, base_delay=0.001))
    assert calls == 1


@pytest.mark.asyncio
async def test_unlisted_exceptions_propagate_immediately():
    async def wrong_type():
        raise KeyError("k")

    with pytest.raises(KeyError):
        await retry_async(wrong_type, retry_on=(ConnectionError,),
                          policy=RetryPolicy(attempts=3, base_delay=0.001))


def test_backoff_grows_and_is_capped():
    policy = RetryPolicy(base_delay=1.0, factor=2.0, max_delay=5.0, jitter=False)
    assert [policy.delay_for(i) for i in range(1, 5)] == [1.0, 2.0, 4.0, 5.0]


def test_jitter_stays_within_the_backoff_bound():
    policy = RetryPolicy(base_delay=1.0, factor=2.0, max_delay=8.0, jitter=True)
    assert all(0.0 <= policy.delay_for(3) <= 4.0 for _ in range(50))


# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────

def test_breaker_opens_after_consecutive_failures():
    breaker = CircuitBreaker(threshold=3, reset_after=60.0)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True
    assert breaker.seconds_until_reset() > 0


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.is_open is False


def test_breaker_half_opens_after_the_reset_window():
    breaker = CircuitBreaker(threshold=1, reset_after=0.0)
    breaker.record_failure()
    assert breaker.is_open is False        # window already elapsed, probe allowed
    assert breaker.seconds_until_reset() == 0.0


# ─────────────────────────────────────────────
# BOUNDED GATHER
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gather_bounded_caps_concurrency_and_returns_errors():
    active = 0
    peak = 0

    async def work(n: int):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        if n == 2:
            raise RuntimeError("bad")
        return n

    results = await gather_bounded((work(i) for i in range(6)), limit=2)
    assert peak <= 2
    assert isinstance(results[2], RuntimeError)
    assert results[0] == 0
