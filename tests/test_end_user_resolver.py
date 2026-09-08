"""Resolving an identity on the request path, and the counters that ride along.

Three properties, and each is about the request rather than about the row: the resolver
costs nothing when it already knows the answer, it never raises into a completion, and the
counters it feeds are batched rather than written per request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.tenancy import TenantScope
from app.services.end_user import ANONYMOUS, FROM_HEADER, EndUserIdentity
from app.services.end_user_resolver import EndUserResolver, RequestCounters
from tests.auth_support import make_organization
from tests.end_user_support import EndUserFixture, build_end_users


@pytest.fixture
def memory() -> EndUserFixture:
    return build_end_users(make_organization())


def named(external_id: str = "alice", source: str = FROM_HEADER) -> EndUserIdentity:
    return EndUserIdentity(external_id=external_id, source=source)


class CountingStore:
    """The real store, with every ``touch`` counted."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.touches = 0

    def begin(self, scope: TenantScope) -> object:
        return _CountingTransaction(self, scope)


class _CountingTransaction:
    def __init__(self, outer: CountingStore, scope: TenantScope) -> None:
        self._outer = outer
        self._scope = scope
        self._inner: object = None

    async def __aenter__(self) -> _CountingTransaction:
        self._context = self._outer.inner.begin(self._scope)  # type: ignore[attr-defined]
        self._inner = await self._context.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._context.__aexit__(*args)

    async def touch(self, external_id: str) -> object:
        self._outer.touches += 1
        return await self._inner.touch(external_id)  # type: ignore[attr-defined]

    async def commit(self) -> None:
        await self._inner.commit()  # type: ignore[attr-defined]


class BrokenStore:
    def begin(self, scope: TenantScope) -> object:
        raise RuntimeError("the database is unreachable")


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


async def test_a_new_identity_gets_a_row(memory: EndUserFixture) -> None:
    resolved = await memory.resolver.resolve(
        organization_id=memory.organization_id, identity=named()
    )

    assert resolved is not None
    assert resolved.external_id == "alice"
    async with memory.store.begin(memory.actor.scope) as transaction:
        assert await transaction.end_user(resolved.id) is not None


async def test_no_identity_resolves_to_nobody(memory: EndUserFixture) -> None:
    assert (
        await memory.resolver.resolve(organization_id=memory.organization_id, identity=None) is None
    )


async def test_a_repeated_identity_costs_no_database_work(memory: EndUserFixture) -> None:
    """The whole reason this is affordable in front of every completion."""
    counting = CountingStore(memory.store)
    resolver = EndUserResolver(counting)  # type: ignore[arg-type]

    for _ in range(5):
        await resolver.resolve(organization_id=memory.organization_id, identity=named())

    assert counting.touches == 1


async def test_the_cache_is_per_organization(memory: EndUserFixture) -> None:
    """Two customers both have an ``alice``; a cache keyed on the name alone would give
    the second one the first one's memory."""
    other = build_end_users(
        make_organization(name="Globex", slug="globex"), database=memory.database
    )
    resolver = EndUserResolver(memory.store)

    mine = await resolver.resolve(organization_id=memory.organization_id, identity=named())
    theirs = await resolver.resolve(organization_id=other.organization_id, identity=named())

    assert mine is not None and theirs is not None
    assert mine.id != theirs.id


async def test_a_cached_entry_expires(memory: EndUserFixture) -> None:
    """The TTL exists so a replica eventually forgets a purged end user; correctness does
    not depend on it, which is why it is generous."""
    counting = CountingStore(memory.store)
    resolver = EndUserResolver(counting, ttl_seconds=0.0)  # type: ignore[arg-type]

    await resolver.resolve(organization_id=memory.organization_id, identity=named())
    await resolver.resolve(organization_id=memory.organization_id, identity=named())

    assert counting.touches == 2


async def test_forgetting_an_identity_drops_it_from_the_cache(memory: EndUserFixture) -> None:
    counting = CountingStore(memory.store)
    resolver = EndUserResolver(counting)  # type: ignore[arg-type]

    await resolver.resolve(organization_id=memory.organization_id, identity=named())
    resolver.forget(memory.organization_id, "alice")
    await resolver.resolve(organization_id=memory.organization_id, identity=named())

    assert counting.touches == 2


async def test_the_cache_is_bounded(memory: EndUserFixture) -> None:
    """A customer generating a fresh anonymous id per request must not grow it forever."""
    resolver = EndUserResolver(memory.store, max_entries=2)

    for index in range(5):
        await resolver.resolve(
            organization_id=memory.organization_id, identity=named(f"caller-{index}")
        )

    # Reaching into the private map on purpose: the property is that the map stays
    # bounded, and there is no public way to observe a cache that is working.
    assert len(resolver._cache) <= 2


async def test_a_database_failure_costs_the_memory_not_the_request(
    memory: EndUserFixture,
) -> None:
    """A fail-closed gateway's policy is about the knowledge base being unreachable.
    Turning "PostgreSQL blinked" into a 503 would be a much larger outage."""
    resolver = EndUserResolver(BrokenStore())  # type: ignore[arg-type]

    resolved = await resolver.resolve(organization_id=memory.organization_id, identity=named())

    assert resolved is None


async def test_an_anonymous_identity_is_marked_as_one(memory: EndUserFixture) -> None:
    resolved = await memory.resolver.resolve(
        organization_id=memory.organization_id,
        identity=named("anon:abc123", source=ANONYMOUS),
    )

    assert resolved is not None
    assert resolved.anonymous


# ---------------------------------------------------------------------------
# counters
# ---------------------------------------------------------------------------


async def test_sightings_are_batched_rather_than_written_per_request(
    memory: EndUserFixture,
) -> None:
    """Per-request writes would turn every completion into an update on a hot row."""
    resolved = await memory.resolver.resolve(
        organization_id=memory.organization_id, identity=named()
    )
    assert resolved is not None
    for _ in range(9):
        await memory.resolver.resolve(organization_id=memory.organization_id, identity=named())

    async with memory.store.begin(memory.actor.scope) as transaction:
        before = await transaction.end_user(resolved.id)
        assert before is not None
        assert before.request_count == 0

    written = await memory.counters.flush()

    async with memory.store.begin(memory.actor.scope) as transaction:
        after = await transaction.end_user(resolved.id)
        assert after is not None
        assert (written, after.request_count) == (1, 10)


async def test_a_flush_with_nothing_pending_writes_nothing(memory: EndUserFixture) -> None:
    assert await memory.counters.flush() == 0


async def test_a_failing_flush_drops_the_batch_rather_than_retrying(
    memory: EndUserFixture,
) -> None:
    """A retry loop in front of an unbounded tally is how a database outage becomes a
    memory leak, and the thing being lost is a request count."""
    counters = RequestCounters(BrokenStore(), interval_seconds=3600.0)  # type: ignore[arg-type]
    counters.record(memory.organization_id, uuid.uuid4())

    assert await counters.flush() == 0
    assert await counters.flush() == 0


async def test_counters_stop_by_flushing_what_is_left(memory: EndUserFixture) -> None:
    """A rolling deploy stops processes constantly; without the final flush every deploy
    would drop the last window of every replica's sightings."""
    resolved = await memory.resolver.resolve(
        organization_id=memory.organization_id, identity=named()
    )
    assert resolved is not None

    await memory.counters.stop()

    async with memory.store.begin(memory.actor.scope) as transaction:
        row = await transaction.end_user(resolved.id)
        assert row is not None
        assert row.request_count == 1


async def test_a_sighting_moves_last_seen_forward(memory: EndUserFixture) -> None:
    resolved = await memory.resolver.resolve(
        organization_id=memory.organization_id, identity=named()
    )
    assert resolved is not None
    before = datetime.now(UTC)

    await memory.counters.flush()

    async with memory.store.begin(memory.actor.scope) as transaction:
        row = await transaction.end_user(resolved.id)
        assert row is not None
        assert row.last_seen_at >= before
