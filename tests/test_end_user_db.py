"""End users and memory facts against a real PostgreSQL.

Three things that cannot be checked anywhere else: the store contract against the
implementation that ships, the constraints (a CHECK is only real if the server enforces
it), and the ``ON DELETE CASCADE`` that takes a person's facts with them.

The upsert is why this file matters most. ``touch`` is an ``ON CONFLICT ... RETURNING`` in
PostgreSQL and a dictionary lookup in memory, and the property it exists for — two
simultaneous first sightings of one end user produce one row — is a promise about the
first. It is kept by two facts asserted separately below: the unique constraint exists and
the server enforces it, and the upsert converges onto it instead of raising.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import EndUser, MemoryFact
from app.services.end_user_store import PostgresEndUserStore
from tests.end_user_store_contract import CHECKS, Check, Fixture, draft
from tests.test_end_user_store_memory import build_fixture

pytestmark = pytest.mark.db


@pytest.fixture
async def fixture(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Fixture]:
    """The same rows the in-memory half seeds, inserted for real."""
    database, memory = build_fixture()

    for organization in database.organizations.values():
        db_session.add(organization)
    await db_session.flush()
    for end_user in database.end_users.values():
        db_session.add(end_user)
    await db_session.flush()

    yield Fixture(
        store=PostgresEndUserStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_end_user=memory.acme_end_user,
        globex_end_user=memory.globex_end_user,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


# ---------------------------------------------------------------------------
# what only a server can answer
# ---------------------------------------------------------------------------


async def test_one_identity_per_organization_is_a_database_guarantee(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The upsert converges onto this constraint; without it, it would converge onto
    nothing and two racing first sightings would produce two people."""
    db_session.add(
        EndUser(
            id=uuid7(),
            organization_id=fixture.acme.id,
            external_id=fixture.acme_end_user.external_id,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_unknown_fact_kind_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        MemoryFact(
            id=uuid7(),
            organization_id=fixture.acme.id,
            end_user_id=fixture.acme_end_user.id,
            text="Something.",
            kind="rumour",
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_a_confidence_outside_zero_to_one_is_refused(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        MemoryFact(
            id=uuid7(),
            organization_id=fixture.acme.id,
            end_user_id=fixture.acme_end_user.id,
            text="Something.",
            confidence=1.5,
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_an_empty_fact_is_refused(db_session: AsyncSession, fixture: Fixture) -> None:
    db_session.add(
        MemoryFact(
            id=uuid7(),
            organization_id=fixture.acme.id,
            end_user_id=fixture.acme_end_user.id,
            text="",
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_deleting_an_end_user_takes_their_facts(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """``ON DELETE CASCADE``. The memory store emulates it; only the server proves it."""
    store = fixture.store
    async with store.begin(fixture.acme_scope) as transaction:
        await transaction.add_fact(fixture.acme_end_user, draft("Goes away with them."))
        await transaction.commit()

    row = await db_session.get(EndUser, fixture.acme_end_user.id)
    assert row is not None
    await db_session.delete(row)
    await db_session.flush()

    remaining = await db_session.execute(
        select(func.count())
        .select_from(MemoryFact)
        .where(MemoryFact.end_user_id == fixture.acme_end_user.id)
        .execution_options(tenant_scope="bypass:asserting the cascade removed every row")
    )
    assert remaining.scalar() == 0


async def test_the_liveness_predicate_is_the_servers_and_not_pythons(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """``expires_at IS NULL`` is the common case, and ``NULL > now()`` is null, not true.

    Getting that wrong in SQL would silently stop injecting every fact that has no expiry
    at all — which is almost all of them — and nothing would raise.
    """
    store = fixture.store
    async with store.begin(fixture.acme_scope) as transaction:
        forever = await transaction.add_fact(fixture.acme_end_user, draft("No expiry."))
        later = await transaction.add_fact(
            fixture.acme_end_user,
            draft("Expires later.", expires_at=datetime.now(UTC) + timedelta(days=7)),
        )
        await transaction.commit()

    async with store.begin(fixture.acme_scope) as transaction:
        found = await transaction.live_facts(
            fixture.acme_end_user.id, [forever.id, later.id], now=datetime.now(UTC)
        )

    assert {row.id for row in found} == {forever.id, later.id}


async def test_the_scope_guard_covers_both_new_tables(db_session: AsyncSession) -> None:
    """Both carry ``organization_id``, so an unscoped read of either must raise rather
    than quietly returning every tenant's rows."""
    from app.db.scoping import UnscopedQuery

    for model in (EndUser, MemoryFact):
        with pytest.raises(UnscopedQuery):
            await db_session.execute(select(model))


async def test_a_scoped_read_is_restricted_to_its_organization(
    db_session_factory: async_sessionmaker[AsyncSession], fixture: Fixture
) -> None:
    scope = TenantScope(role="org_admin", organization_id=fixture.acme.id)
    async with fixture.store.begin(scope) as transaction:
        rows = await transaction.end_users(after=None, limit=50)

    assert {row.organization_id for row in rows} == {fixture.acme.id}
