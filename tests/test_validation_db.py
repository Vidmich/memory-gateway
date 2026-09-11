"""The validation stores against a real PostgreSQL (task 103).

``DISTINCT ON`` for the latest audit per connector and kind, the filtered counts per set,
and the cascades from a set to its items and runs are the things worth a server: each is
right by iteration in the memory twin and right in SQL only if written correctly. Plus the
CHECKs, which are only real if the server enforces them.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.evaluation_store import PostgresEvaluationStore
from app.services.index_audit_store import PostgresIndexAuditStore
from tests.test_validation_store_memory import build_fixture
from tests.validation_store_contract import CHECKS, Check, Fixture
from tests.validation_support import make_evaluation_item, make_index_audit

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
    for gateway in database.gateways.values():
        db_session.add(gateway)
    await db_session.flush()

    yield Fixture(
        audits=PostgresIndexAuditStore(db_session_factory),
        evaluations=PostgresEvaluationStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_connector=memory.acme_connector,
        globex_connector=memory.globex_connector,
        acme_gateway=memory.acme_gateway,
        globex_gateway=memory.globex_gateway,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


async def test_an_unknown_audit_kind_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(make_index_audit(fixture.acme_connector.id, fixture.acme.id, kind="vibes"))

    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_an_unknown_item_source_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    from tests.validation_support import make_evaluation_set

    evaluation_set = make_evaluation_set(fixture.acme_gateway)
    db_session.add(evaluation_set)
    await db_session.flush()
    db_session.add(make_evaluation_item(evaluation_set, source="hearsay"))

    with pytest.raises(DBAPIError):
        await db_session.flush()
