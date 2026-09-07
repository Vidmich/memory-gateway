"""The fixed-window limiter that sits in front of "Test connection".

Two properties matter and one of them is uncomfortable. It has to stop a caller who
presses the button in a loop, and it has to **fail open** when the counter store is
unreachable — the same trade the login throttle makes, for the same reason: a Redis
hiccup should not make the configuration screen unusable during an incident. That second
one is easy to break accidentally, so it has a test rather than a comment.
"""

from __future__ import annotations

import pytest

from app.core.errors import RateLimited
from app.services.login_throttle import MemoryThrottleStore
from app.services.rate_limit import FixedWindowLimiter


class BrokenStore:
    """Every operation raises, as a refused Redis connection would."""

    async def increment(self, key: str, ttl_seconds: int) -> int:
        raise ConnectionError("redis is down")

    async def peek(self, key: str) -> tuple[int, int]:
        raise ConnectionError("redis is down")

    async def delete(self, key: str) -> None:
        raise ConnectionError("redis is down")


def build(limit: int = 3) -> FixedWindowLimiter:
    return FixedWindowLimiter(
        store=MemoryThrottleStore(), action="model-test", limit=limit, window_seconds=60
    )


async def test_actions_up_to_the_limit_are_allowed() -> None:
    limiter = build(limit=3)

    for _ in range(3):
        await limiter.check("user-1")


async def test_the_next_one_is_refused() -> None:
    limiter = build(limit=3)
    for _ in range(3):
        await limiter.check("user-1")

    with pytest.raises(RateLimited):
        await limiter.check("user-1")


async def test_the_refusal_carries_a_retry_after_header() -> None:
    """A 429 without one leaves the client guessing, and the guess is "immediately"."""
    limiter = build(limit=1)
    await limiter.check("user-1")

    with pytest.raises(RateLimited) as failure:
        await limiter.check("user-1")

    assert failure.value.status_code == 429
    assert int(failure.value.headers["retry-after"]) > 0


async def test_the_counter_is_per_subject() -> None:
    """One user exhausting their budget must not lock out their colleague."""
    limiter = build(limit=1)
    await limiter.check("user-1")

    await limiter.check("user-2")


async def test_two_actions_do_not_share_a_counter() -> None:
    store = MemoryThrottleStore()
    testing = FixedWindowLimiter(store=store, action="model-test", limit=1, window_seconds=60)
    other = FixedWindowLimiter(store=store, action="something-else", limit=1, window_seconds=60)
    await testing.check("user-1")

    await other.check("user-1")


async def test_an_unreachable_store_allows_the_action() -> None:
    """Fails open, deliberately. The exposure is bounded by what the action costs, which
    for a probe is one token."""
    limiter = FixedWindowLimiter(
        store=BrokenStore(), action="model-test", limit=1, window_seconds=60
    )

    for _ in range(10):
        await limiter.check("user-1")
