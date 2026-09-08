"""The audit log against a real PostgreSQL.

Two things that cannot be checked anywhere else.

The store contract against the implementation that ships — the scope clause, the filters,
the half-open window and the cursor, expressed as SQL rather than as list comprehensions.

And **immutability**, which is the property this whole task rests on. The repository
exposes no way to change an event and the service has no endpoint for it, but both of
those are conventions that hold until somebody adds a method. The trigger installed by
``0014_audit_log`` is what holds afterwards, and it can only be demonstrated against a
server that has it.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AuditEvent
from app.db.scoping import unscoped
from app.services.audit_store import PostgresAuditStore
from tests.audit_store_contract import CHECKS, Check, Fixture
from tests.test_audit_store_memory import build_fixture

pytestmark = pytest.mark.db


@pytest.fixture
async def fixture(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Fixture]:
    """The same rows the in-memory half seeds, inserted for real.

    No ``organizations`` rows: ``audit_events`` deliberately carries no foreign key, so
    the ids stand alone. That the insert succeeds is itself part of what is being checked
    — the log has to outlive its subjects, including the organization.
    """
    database, memory = build_fixture()
    for event in database.audit_events.values():
        db_session.add(event)
    await db_session.flush()
    await db_session.commit()

    yield Fixture(
        store=PostgresAuditStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_event=memory.acme_event,
        acme_older=memory.acme_older,
        globex_event=memory.globex_event,
        platform_event=memory.platform_event,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


async def test_the_database_refuses_to_update_an_event(
    fixture: Fixture, db_session: AsyncSession
) -> None:
    """The backstop. A convention holds until somebody writes the line that breaks it,
    and by then the log has already been trusted."""
    with pytest.raises(DBAPIError) as raised:
        await db_session.execute(
            update(AuditEvent)
            .where(AuditEvent.id == fixture.acme_event.id)
            .values(action="something.else")
            .execution_options(**unscoped("deliberately attempting a forbidden write"))
        )
    assert "append-only" in str(raised.value)
    await db_session.rollback()


async def test_the_database_refuses_to_delete_an_event(
    fixture: Fixture, db_session: AsyncSession
) -> None:
    with pytest.raises(DBAPIError) as raised:
        await db_session.execute(
            delete(AuditEvent)
            .where(AuditEvent.id == fixture.acme_event.id)
            .execution_options(**unscoped("deliberately attempting a forbidden write"))
        )
    assert "append-only" in str(raised.value)
    await db_session.rollback()


async def test_the_event_survives_its_organization_being_deleted(
    fixture: Fixture, db_session: AsyncSession
) -> None:
    """There is no cascade to take it, which is the whole reason there is no foreign key.

    A cascade here would also be a ``DELETE`` the trigger refuses, so the choice was
    between a table that cannot be tidied up and a customer record that disappears with
    the customer. Neither is what a log is for; this is the third option.
    """
    statement = (
        select(AuditEvent)
        .where(AuditEvent.organization_id == fixture.acme)
        .execution_options(**unscoped("reading the log without a tenant scope, on purpose"))
    )
    assert len((await db_session.execute(statement)).scalars().all()) == 2

    columns = await db_session.execute(
        text(
            "SELECT count(*) FROM information_schema.table_constraints "
            "WHERE table_name = 'audit_events' AND constraint_type = 'FOREIGN KEY'"
        )
    )
    assert columns.scalar() == 0
