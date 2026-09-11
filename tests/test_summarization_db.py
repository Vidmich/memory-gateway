"""The summarization ledger against a real PostgreSQL (task 102).

The grouping is the thing worth a server: three ``GROUP BY`` queries and an outer join to
``connectors`` for the names, each of which the in-memory twin gets right by iteration and
the SQL gets right only if written correctly. Plus the two CHECKs, which are only real if
the server enforces them.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import SummarizationRun
from app.services.summarization_store import SUCCEEDED, PostgresSummarizationStore
from tests.summarization_store_contract import CHECKS, Check, Fixture
from tests.test_summarization_store_memory import build_fixture

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
    for document in database.documents.values():
        db_session.add(document)
    await db_session.flush()

    yield Fixture(
        store=PostgresSummarizationStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_connector=memory.acme_connector,
        acme_other=memory.acme_other,
        globex_connector=memory.globex_connector,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


async def test_an_unknown_outcome_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        SummarizationRun(
            id=uuid7(),
            organization_id=fixture.acme.id,
            connector_id=fixture.acme_connector.id,
            document_id=uuid7(),
            outcome="probably",
        )
    )

    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_a_negative_token_count_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The tokens are a bill. A negative line on a bill is not a number anybody meant."""
    db_session.add(
        SummarizationRun(
            id=uuid7(),
            organization_id=fixture.acme.id,
            connector_id=fixture.acme_connector.id,
            document_id=uuid7(),
            outcome=SUCCEEDED,
            tokens_in=-1,
        )
    )

    with pytest.raises(DBAPIError):
        await db_session.flush()
