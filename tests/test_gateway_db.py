"""Gateways and keys against a real PostgreSQL.

Four things that cannot be checked anywhere else: the store contract against the
implementation that ships, the constraints (a CHECK is only real if the server enforces
it), the ``ON DELETE CASCADE`` that takes a gateway's keys with it, and the scope guard —
which, for ``api_keys``, is the thing that *does not* protect us, because the table has no
``organization_id`` column. That last one is the whole reason
:class:`~app.db.repositories.ApiKeyRepository` exists, so it is asserted here rather than
assumed.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ApiKey, Gateway
from app.services.gateway_store import PostgresGatewayStore
from tests.gateway_store_contract import CHECKS, Check, Fixture
from tests.gateway_support import make_gateway_row, make_key_row
from tests.test_gateway_store_memory import build_fixture

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
    for model in database.upstream_models.values():
        db_session.add(model)
    for gateway in database.gateways.values():
        db_session.add(gateway)
    await db_session.flush()
    for target in database.gateway_targets.values():
        db_session.add(target)
    for key in database.api_keys.values():
        db_session.add(key)
    await db_session.flush()

    yield Fixture(
        store=PostgresGatewayStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_model=memory.acme_model,
        globex_model=memory.globex_model,
        global_model=memory.global_model,
        acme_gateway=memory.acme_gateway,
        globex_gateway=memory.globex_gateway,
        acme_key=memory.acme_key,
        globex_key=memory.globex_key,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


# ---------------------------------------------------------------------------
# constraints
# ---------------------------------------------------------------------------


async def test_a_slug_is_unique_across_organizations(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The service checks this first and answers with a sentence. The constraint is what
    holds when two requests race, which is the case the check cannot cover."""
    db_session.add(make_gateway_row(fixture.globex, slug="acme-chat"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "slug",
    ["ab", "-leading", "trailing-", "Upper", "has space", "has_underscore", "dot.slug"],
)
async def test_a_malformed_slug_is_refused_by_the_database(
    slug: str, db_session: AsyncSession, fixture: Fixture
) -> None:
    """A slug is a path segment on a public URL. The service produces the readable
    message; this is what catches a row written by a script that skipped it."""
    db_session.add(make_gateway_row(fixture.acme, slug=slug))

    with pytest.raises((IntegrityError, DBAPIError)):
        await db_session.flush()


async def test_an_unknown_routing_mode_is_refused(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(make_gateway_row(fixture.acme, slug="bad-mode", routing_mode="round_robin"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_the_three_accepted_routing_modes_all_store(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The CHECK is the backstop under the service's own validation: it is what stops a
    migration or a script from writing a mode the router has never heard of."""
    for index, mode in enumerate(("single", "failover", "ab_split")):
        db_session.add(make_gateway_row(fixture.acme, slug=f"mode-{index}", routing_mode=mode))
    await db_session.flush()


async def test_deleting_a_gateway_cascades_to_its_keys(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    gateway = await db_session.get(Gateway, fixture.acme_gateway.id)
    assert gateway is not None
    await db_session.delete(gateway)
    await db_session.flush()

    remaining = (
        await db_session.execute(
            select(ApiKey.id).where(ApiKey.gateway_id == fixture.acme_gateway.id)
        )
    ).all()
    assert remaining == []


async def test_a_key_needs_a_gateway_that_exists(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    from app.core.ids import uuid7

    key, _ = make_key_row(uuid7(), name="orphan")
    db_session.add(key)

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_key_hash_is_unique(db_session: AsyncSession, fixture: Fixture) -> None:
    """Two rows with the same hash would make authentication ambiguous, and the token
    embeds a row id — so the pair could disagree about which key was used."""
    existing = await db_session.get(ApiKey, fixture.acme_key.id)
    assert existing is not None
    duplicate, _ = make_key_row(fixture.acme_gateway.id, name="clone")
    duplicate.key_hash = existing.key_hash
    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# the scope guard, and where it does not reach
# ---------------------------------------------------------------------------


async def test_the_guard_still_refuses_an_unscoped_gateway_query(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    from app.db.scoping import UnscopedQuery

    with pytest.raises(UnscopedQuery):
        await db_session.execute(select(Gateway))


async def test_the_guard_does_not_cover_api_keys(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """Deliberately asserting the *gap*.

    ``api_keys`` has no ``organization_id``, and the guard derives its list from that
    column, so a bare select sails through. That is why every key read goes through
    :class:`~app.db.repositories.ApiKeyRepository`, which joins ``gateways``. If this test
    ever starts failing because the guard grew to cover the table, the repository's join
    becomes belt-and-braces rather than the only belt — good news, worth noticing.
    """
    rows = (await db_session.execute(select(ApiKey.id))).all()

    assert len(rows) == 2
