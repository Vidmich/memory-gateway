"""The reprocessing stores against a real PostgreSQL (task 104).

The per-format ``UPDATE`` with its media-type clause, the grouped counts, the scope
selection, the locked counter increment, and the ``regexp_replace`` at a model swap are
right by iteration in the memory twin and right in SQL only if written correctly. Plus
the CHECKs, which are only real if the server enforces them.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.connector_store import PostgresConnectorStore
from app.services.reprocessing_store import PostgresReprocessingStore
from tests.reprocessing_store_contract import CHECKS, Check, Fixture
from tests.reprocessing_support import make_reprocessing_run
from tests.test_reprocessing_store_memory import build_fixture

pytestmark = pytest.mark.db


@pytest.fixture
async def fixture(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Fixture]:
    database, memory = build_fixture()
    for organization in database.organizations.values():
        db_session.add(organization)
    await db_session.flush()
    for connector in database.connectors.values():
        db_session.add(connector)
    await db_session.flush()

    yield Fixture(
        connectors=PostgresConnectorStore(db_session_factory),
        runs=PostgresReprocessingStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_connector=memory.acme_connector,
        globex_connector=memory.globex_connector,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


async def test_an_unknown_trigger_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(make_reprocessing_run(fixture.acme_connector, trigger="vibes"))

    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_an_unknown_index_status_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    from tests.connector_support import make_document

    document = make_document(fixture.acme_connector)
    document.index_status = "confused"
    db_session.add(document)

    with pytest.raises(DBAPIError):
        await db_session.flush()
