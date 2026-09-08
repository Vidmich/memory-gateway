"""One set of checks, run against both limit stores.

The whole point of this file is that the in-memory store and the Lua script have to agree.
Everything else about rate limiting is tested against the fast one; if the two ever drift,
every one of those tests is measuring something production does not do.

``now`` is a parameter of every call rather than a clock, so a window boundary and a
concurrency lease expiring are ordinary arithmetic instead of a sleep. That is also how the
real store works — see the note about one clock in :mod:`app.services.limit_store`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from app.services.limit_store import Consumption
from app.services.limits import Rule, bucket_key

MINUTE = 60
DAY = 24 * 3600


def rule(
    limit: str = "requests_per_minute",
    *,
    value: int = 10,
    window: int = MINUTE,
    scope: str = "gateway",
    key: str | None = None,
) -> Rule:
    return Rule(
        scope=scope,  # type: ignore[arg-type]
        limit=limit,
        value=value,
        key=key or bucket_key(limit, gateway_id=uuid.uuid4()),
        window_seconds=window,
    )


def spend(store: Any, rules: Sequence[Rule], *, cost: int = 1, holder: str, now: float) -> Any:
    return store.consume(
        [Consumption(rule=item, cost=cost) for item in rules], holder=holder, now=now
    )


async def check_store(store: Any) -> None:
    """Every property both implementations must have. Raises on the first failure."""
    await _admits_exactly_the_limit(store)
    await _a_rejection_consumes_nothing_anywhere(store)
    await _separate_keys_do_not_share_a_budget(store)
    await _a_window_slides_rather_than_resetting(store)
    await _a_burst_across_a_boundary_cannot_double_the_limit(store)
    await _tokens_are_spent_by_cost_not_by_call(store)
    await _settlement_corrects_upwards(store)
    await _settlement_corrects_downwards_and_floors_at_zero(store)
    await _settlement_lands_in_the_window_it_is_made_in(store)
    await _a_concurrency_slot_is_held_until_released(store)
    await _a_leaked_concurrency_slot_expires_on_its_own(store)
    await _releasing_twice_is_harmless(store)
    await _peek_consumes_nothing(store)
    await _reset_counts_down_within_the_window(store)


# ---------------------------------------------------------------------------


async def _admits_exactly_the_limit(store: Any) -> None:
    """SPEC §11's whole promise, and the acceptance criterion the Lua script exists for."""
    one = rule(value=5)
    now = 1_000_000.0
    for index in range(5):
        outcome = await spend(store, [one], holder=f"h{index}", now=now)
        assert outcome.allowed, f"request {index + 1} of 5 was refused"

    outcome = await spend(store, [one], holder="h5", now=now)
    assert not outcome.allowed
    assert outcome.refused is not None
    assert outcome.refused.rule is one
    assert outcome.refused.remaining == 0


async def _a_rejection_consumes_nothing_anywhere(store: Any) -> None:
    """The reason check-and-consume is one script.

    A request refused by the second rule must not have spent the first. Without that, a
    caller who is over their personal cap quietly burns the gateway's minute on every
    attempt — and the gateway's chart shows traffic that was never served.
    """
    roomy = rule(value=100)
    tight = rule(value=1, scope="end_user")
    now = 2_000_000.0
    await spend(store, [roomy, tight], holder="first", now=now)

    outcome = await spend(store, [roomy, tight], holder="second", now=now)
    assert not outcome.allowed

    after = await store.peek([roomy], now=now)
    assert after[0].remaining == 99, "the gateway's budget moved on a refused request"


async def _separate_keys_do_not_share_a_budget(store: Any) -> None:
    first = rule(value=1)
    second = rule(value=1)
    now = 3_000_000.0
    await spend(store, [first], holder="a", now=now)

    outcome = await spend(store, [second], holder="a", now=now)
    assert outcome.allowed


async def _a_window_slides_rather_than_resetting(store: Any) -> None:
    """Half a window later, half the previous window's spend is still counted."""
    one = rule(value=10)
    start = 4_000_000.0 - (4_000_000.0 % MINUTE)
    for index in range(10):
        await spend(store, [one], holder=f"h{index}", now=start + 1)

    # Thirty seconds into the *next* window: five of the ten are still inside the
    # trailing minute, so there is room for five and not for six.
    later = start + MINUTE + 30
    for index in range(5):
        outcome = await spend(store, [one], holder=f"n{index}", now=later)
        assert outcome.allowed, f"{index + 1} of 5 refused half a window later"

    outcome = await spend(store, [one], holder="n5", now=later)
    assert not outcome.allowed


async def _a_burst_across_a_boundary_cannot_double_the_limit(store: Any) -> None:
    """The failure a fixed window has: ten at 11:59:59 and ten at 12:00:00."""
    one = rule(value=10)
    start = 5_000_000.0 - (5_000_000.0 % MINUTE)
    for index in range(10):
        assert (await spend(store, [one], holder=f"a{index}", now=start + MINUTE - 1)).allowed

    admitted = 0
    for index in range(10):
        if (await spend(store, [one], holder=f"b{index}", now=start + MINUTE + 1)).allowed:
            admitted += 1
    assert admitted <= 1, f"{admitted} extra requests slipped across the boundary"


async def _tokens_are_spent_by_cost_not_by_call(store: Any) -> None:
    one = rule("tokens_per_minute", value=1000)
    now = 6_000_000.0
    assert (await spend(store, [one], cost=900, holder="a", now=now)).allowed

    outcome = await spend(store, [one], cost=200, holder="b", now=now)
    assert not outcome.allowed
    assert (await spend(store, [one], cost=100, holder="c", now=now)).allowed


async def _settlement_corrects_upwards(store: Any) -> None:
    """The estimate was low: the difference comes out of the same bucket."""
    one = rule("tokens_per_minute", value=1000)
    now = 7_000_000.0
    await spend(store, [one], cost=100, holder="a", now=now)

    await store.settle([(one, 400)], now=now)

    readings = await store.peek([one], now=now)
    assert readings[0].remaining == 500


async def _settlement_corrects_downwards_and_floors_at_zero(store: Any) -> None:
    one = rule("tokens_per_minute", value=1000)
    now = 8_000_000.0
    await spend(store, [one], cost=800, holder="a", now=now)

    await store.settle([(one, -300)], now=now)
    assert (await store.peek([one], now=now))[0].remaining == 500

    # A correction larger than everything counted must not leave a negative bucket, which
    # would hand the next caller more than the limit.
    await store.settle([(one, -5000)], now=now)
    assert (await store.peek([one], now=now))[0].remaining == 1000


async def _settlement_lands_in_the_window_it_is_made_in(store: Any) -> None:
    """SPEC §11's "carried into the next window", which is what this falls out as.

    A response that finishes after the minute it started in settles against the minute it
    finished in — which is the minute its tokens were actually produced.
    """
    one = rule("tokens_per_minute", value=1000)
    start = 9_000_000.0 - (9_000_000.0 % MINUTE)
    await spend(store, [one], cost=100, holder="a", now=start + MINUTE - 1)

    await store.settle([(one, 600)], now=start + MINUTE + 1)

    # Deep into the following window, so the previous bucket's weight is negligible and
    # what is left is the settlement itself.
    readings = await store.peek([one], now=start + 2 * MINUTE - 1)
    assert readings[0].remaining <= 400


async def _a_concurrency_slot_is_held_until_released(store: Any) -> None:
    one = rule("concurrent_requests", value=2, window=0)
    now = 10_000_000.0
    first = await spend(store, [one], holder="one", now=now)
    second = await spend(store, [one], holder="two", now=now)
    assert first.allowed and second.allowed

    assert not (await spend(store, [one], holder="three", now=now)).allowed

    await store.release(first.holds)
    assert (await spend(store, [one], holder="three", now=now)).allowed


async def _a_leaked_concurrency_slot_expires_on_its_own(store: Any) -> None:
    """The failure this design exists to avoid: a counter nobody gives back.

    Nothing releases the first holder. A lease later the slot is reclaimed anyway, which
    is why the store keeps arrival times rather than an integer.
    """
    one = rule("concurrent_requests", value=1, window=0)
    now = 11_000_000.0
    assert (await spend(store, [one], holder="lost", now=now)).allowed
    assert not (await spend(store, [one], holder="next", now=now + 60)).allowed

    assert (await spend(store, [one], holder="next", now=now + 100_000)).allowed


async def _releasing_twice_is_harmless(store: Any) -> None:
    one = rule("concurrent_requests", value=1, window=0)
    now = 12_000_000.0
    outcome = await spend(store, [one], holder="only", now=now)

    await store.release(outcome.holds)
    await store.release(outcome.holds)

    assert (await spend(store, [one], holder="another", now=now)).allowed


async def _peek_consumes_nothing(store: Any) -> None:
    one = rule(value=2)
    now = 13_000_000.0
    for _ in range(20):
        await store.peek([one], now=now)

    assert (await spend(store, [one], holder="a", now=now)).allowed
    assert (await spend(store, [one], holder="b", now=now)).allowed


async def _reset_counts_down_within_the_window(store: Any) -> None:
    one = rule(value=10)
    start = 14_000_000.0 - (14_000_000.0 % MINUTE)

    early = await spend(store, [one], holder="a", now=start + 1)
    late = await spend(store, [one], holder="b", now=start + 50)

    assert early.readings[0].reset_seconds > late.readings[0].reset_seconds
    assert 0 < late.readings[0].reset_seconds <= MINUTE
