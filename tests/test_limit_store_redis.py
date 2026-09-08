"""The Lua script against the same contract, and against concurrency it cannot fake.

Skipped when there is no Redis, like every other test here that needs a live service. It
is the only place the atomicity claim is actually checked: the in-memory store runs in one
event loop where a read-modify-write cannot interleave, so it would pass a non-atomic
implementation without complaint.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.services.limit_store import Consumption, RedisLimitStore
from tests.limit_store_contract import check_store, rule


async def _redis(clients: object) -> Any:
    connection = getattr(clients, "redis", None)
    assert connection is not None
    try:
        await connection.ping()
    except Exception as exc:  # no Redis on this machine
        pytest.skip(f"Redis not available: {exc}")
    return connection


async def test_the_redis_store_satisfies_the_contract(clients: object) -> None:
    await check_store(RedisLimitStore(await _redis(clients)))


async def test_a_hundred_simultaneous_requests_admit_exactly_the_limit(clients: object) -> None:
    """Task 14's acceptance criterion, and the one a multi-command implementation fails.

    A hundred coroutines is not a hundred threads, but every one of them awaits a real
    round trip to a real server, so the reads and writes genuinely interleave — which is
    all the atomicity claim is about.
    """
    store = RedisLimitStore(await _redis(clients))
    one = rule(value=10, key=f"rl:test:{uuid.uuid4()}:requests_per_minute")

    outcomes = await asyncio.gather(
        *(store.consume([Consumption(rule=one)], holder=f"holder-{index}") for index in range(100))
    )

    assert sum(1 for outcome in outcomes if outcome.allowed) == 10


async def test_simultaneous_concurrency_slots_do_not_overfill(clients: object) -> None:
    store = RedisLimitStore(await _redis(clients))
    one = rule(
        "concurrent_requests",
        value=3,
        window=0,
        key=f"rl:test:{uuid.uuid4()}:concurrent_requests",
    )

    outcomes = await asyncio.gather(
        *(store.consume([Consumption(rule=one)], holder=f"holder-{index}") for index in range(50))
    )

    assert sum(1 for outcome in outcomes if outcome.allowed) == 3


async def test_a_rejected_request_leaves_the_other_bucket_untouched(clients: object) -> None:
    """The all-or-nothing property, over a real server rather than a dict."""
    store = RedisLimitStore(await _redis(clients))
    key = uuid.uuid4()
    roomy = rule(value=100, key=f"rl:test:{key}:requests_per_minute")
    tight = rule(value=1, scope="end_user", key=f"rl:test:{key}:eu:requests_per_minute")

    await store.consume([Consumption(rule=roomy), Consumption(rule=tight)], holder="first")
    refused = await store.consume(
        [Consumption(rule=roomy), Consumption(rule=tight)], holder="second"
    )

    assert not refused.allowed
    assert (await store.peek([roomy]))[0].remaining == 99
