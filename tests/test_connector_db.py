"""Connectors and documents against a real PostgreSQL.

Three things that cannot be checked anywhere else: the store contract against the
implementation that ships, the constraints (a CHECK is only real if the server enforces
it), and the ``ON DELETE CASCADE`` that takes a connector's documents with it.

The upsert is the reason this file matters most. ``claim_document`` is an ``ON CONFLICT``
in PostgreSQL and a dictionary lookup in memory, and the acceptance criterion — ingesting
the same file twice concurrently produces one document — is a promise about the first. It
is kept by two facts asserted separately below: the unique constraint exists and the
server enforces it, and the upsert converges onto it instead of raising.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import Connector, Document
from app.services.connector_store import PostgresConnectorStore
from tests.connector_store_contract import CHECKS, Check, Fixture, draft
from tests.test_connector_store_memory import build_fixture

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
    for connector in database.connectors.values():
        db_session.add(connector)
    await db_session.flush()

    yield Fixture(
        store=PostgresConnectorStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_connector=memory.acme_connector,
        globex_connector=memory.globex_connector,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


# ---------------------------------------------------------------------------
# constraints
# ---------------------------------------------------------------------------


async def test_two_documents_cannot_share_a_source_uri(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The constraint the upsert relies on. Without it, ``ON CONFLICT`` has nothing to
    conflict *with* and every concurrent upload makes a second row."""
    for _ in range(2):
        db_session.add(
            Document(
                id=uuid7(),
                organization_id=fixture.acme.id,
                connector_id=fixture.acme_connector.id,
                source_uri="orgs/x/connectors/y/handbook.md",
                source_name="handbook.md",
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_unknown_document_status_is_refused(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """A status the UI has no column for would be a document nobody can act on. The
    service never writes one; this is what stops a script or a migration from doing so."""
    db_session.add(
        Document(
            id=uuid7(),
            organization_id=fixture.acme.id,
            connector_id=fixture.acme_connector.id,
            source_uri="orgs/x/connectors/y/a.md",
            source_name="a.md",
            status="pondering",
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_an_unknown_connector_status_is_refused(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        Connector(
            id=uuid7(),
            organization_id=fixture.acme.id,
            name="Odd",
            type="managed_file_drop",
            status="thinking",
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_an_unknown_connector_type_is_refused(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        Connector(id=uuid7(), organization_id=fixture.acme.id, name="Warehouse", type="sql")
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_two_connectors_in_one_organization_cannot_share_a_name(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        Connector(
            id=uuid7(),
            organization_id=fixture.acme.id,
            name=fixture.acme_connector.name,
            type="managed_file_drop",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_organizations_may_use_the_same_connector_name(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The other half. Uniqueness is per tenant; a global one would leak the existence of
    another customer's connectors through a name collision."""
    db_session.add(
        Connector(
            id=uuid7(),
            organization_id=fixture.globex.id,
            name=fixture.acme_connector.name,
            type="managed_file_drop",
        )
    )

    await db_session.flush()


async def test_a_negative_chunk_count_is_refused(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        Document(
            id=uuid7(),
            organization_id=fixture.acme.id,
            connector_id=fixture.acme_connector.id,
            source_uri="orgs/x/connectors/y/a.md",
            source_name="a.md",
            chunk_count=-1,
        )
    )

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


# ---------------------------------------------------------------------------
# cascades
# ---------------------------------------------------------------------------


async def test_deleting_a_connector_cascades_to_its_documents(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    document = Document(
        id=uuid7(),
        organization_id=fixture.acme.id,
        connector_id=fixture.acme_connector.id,
        source_uri="orgs/x/connectors/y/a.md",
        source_name="a.md",
    )
    db_session.add(document)
    await db_session.flush()

    await db_session.delete(fixture.acme_connector)
    await db_session.flush()

    remaining = await db_session.execute(select(Document).where(Document.id == document.id))
    assert remaining.scalars().first() is None


# ---------------------------------------------------------------------------
# the upsert
# ---------------------------------------------------------------------------


async def test_claiming_the_same_object_twice_converges_on_one_row(
    db_session_factory: async_sessionmaker[AsyncSession], fixture: Fixture
) -> None:
    """The half of "one document" that this fixture can actually prove.

    A genuine race needs two connections and two real commits, and the ``db_session``
    fixture deliberately binds everything to one connection inside one transaction it
    rolls back — so ``asyncio.gather`` here would serialise rather than race, and a test
    that claimed otherwise would be worse than no test.

    What *is* proved is the pair that makes the concurrent case safe, and both halves are
    here: ``uq_documents_connector_id_source_uri`` exists and the database enforces it
    (:func:`test_two_documents_cannot_share_a_source_uri`), and ``claim_document``'s
    ``ON CONFLICT DO UPDATE`` converges onto it rather than raising — which is the
    difference between an upsert and the read-then-insert it replaced.
    """
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None

        first = await transaction.claim_document(connector, draft(etag="e1"), reset=True)
        second = await transaction.claim_document(connector, draft(etag="e2"), reset=True)
        await transaction.commit()

        rows = await transaction.documents(fixture.acme_connector.id, after=None, limit=50)

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].etag == "e2"


async def test_a_claim_that_conflicts_does_not_raise(
    db_session_factory: async_sessionmaker[AsyncSession], fixture: Fixture
) -> None:
    """The specific failure a read-then-insert produces under concurrency: the losing
    writer gets an ``IntegrityError`` from a constraint it checked a moment earlier."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        await transaction.claim_document(connector, draft(), reset=True)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        # No `pytest.raises`: not raising is the assertion.
        await transaction.claim_document(connector, draft(), reset=False)
        await transaction.commit()
