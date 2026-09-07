"""The model catalog against a real PostgreSQL.

Three things that cannot be checked anywhere else: the store contract against the
implementation that ships, the constraints (a CHECK or a partial unique index is only real
if the server enforces it), and the ``ON DELETE RESTRICT`` that backs the "this model is
in use" refusal — the service's check is a courtesy that produces a good message, and
this is the thing that would still hold if somebody deleted the row another way.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import UpstreamModel
from app.db.scoping import UnscopedQuery
from app.services.catalog import CatalogService, ModelPatch
from app.services.catalog_store import PostgresCatalogStore
from app.services.model_probe import ProbeResult
from tests.catalog_store_contract import CHECKS, Check, Fixture
from tests.catalog_support import FakeProbe, make_model
from tests.test_catalog_store_memory import build_fixture

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
    await db_session.flush()

    yield Fixture(
        store=PostgresCatalogStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_model=memory.acme_model,
        globex_model=memory.globex_model,
        global_model=memory.global_model,
        acme_gateway=memory.acme_gateway,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


# ---------------------------------------------------------------------------
# constraints
# ---------------------------------------------------------------------------


async def test_a_global_model_may_not_name_an_organization(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """``scope_matches_organization``. SPEC §5.3 makes this an isolation boundary, so it
    is a CHECK rather than a convention."""
    db_session.add(make_model(organization=fixture.acme, name="contradictory", scope="global"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_org_model_must_name_an_organization(db_session: AsyncSession) -> None:
    db_session.add(make_model(organization=None, name="ownerless", scope="org"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_global_models_cannot_share_a_name(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """A NULL never collides in a UNIQUE constraint, so the global namespace needs the
    partial index — which only exists on the server."""
    db_session.add(make_model(organization=None, name=fixture.global_model.name))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_models_in_one_organization_cannot_share_a_name(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(make_model(organization=fixture.acme, name=fixture.acme_model.name))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_organizations_can_share_a_model_name(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(make_model(organization=fixture.globex, name=fixture.acme_model.name))

    await db_session.flush()


async def test_an_unknown_dialect_is_refused_by_the_database(db_session: AsyncSession) -> None:
    db_session.add(make_model(organization=None, name="bedrock-model", dialect="bedrock"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_zero_timeout_is_refused(db_session: AsyncSession) -> None:
    db_session.add(make_model(organization=None, name="instant", timeout_seconds=0))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_referenced_model_cannot_be_deleted_at_the_database_level(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """``ON DELETE RESTRICT``. The service's check produces the good message; this is
    what holds if a row is ever deleted some other way."""
    model = await db_session.get(UpstreamModel, fixture.acme_model.id)
    assert model is not None
    await db_session.delete(model)

    with pytest.raises(IntegrityError):
        await db_session.flush()


# ---------------------------------------------------------------------------
# scoping, for real
# ---------------------------------------------------------------------------


async def test_the_guard_fires_on_an_undeclared_model_query(db_session: AsyncSession) -> None:
    """``upstream_models`` carries ``organization_id``, so it is guarded automatically —
    no list to add it to."""
    with pytest.raises(UnscopedQuery):
        await db_session.execute(select(UpstreamModel))


async def test_the_scoped_read_is_actually_filtered_in_sql(
    db_session_factory: async_sessionmaker[AsyncSession], fixture: Fixture
) -> None:
    """The in-memory store filters in Python; this is the one that proves the ``WHERE``
    is really there."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.models(after=None, limit=50)

    assert {row.id for row in rows} == {fixture.acme_model.id, fixture.global_model.id}


# ---------------------------------------------------------------------------
# the service, end to end
# ---------------------------------------------------------------------------


async def test_a_credential_round_trips_through_the_database(
    db_session_factory: async_sessionmaker[AsyncSession], fixture: Fixture
) -> None:
    """Encrypted on write, decrypted for use, and the ciphertext really survived a column
    round trip — ``LargeBinary`` is the kind of thing that works until it does not."""
    secret_box = SecretBox(master_key=os.urandom(32))
    probe = FakeProbe(result=ProbeResult(ok=True, latency_ms=1))
    service = CatalogService(
        PostgresCatalogStore(db_session_factory), secret_box=secret_box, probe=probe
    )
    actor = Actor(
        user_id=uuid7(),
        scope=TenantScope(role="org_admin", organization_id=fixture.acme.id),
    )

    await service.update_model(
        actor, fixture.acme_model.id, ModelPatch(credential="sk-through-postgres-0000")
    )
    await service.test_model(actor, fixture.acme_model.id)

    assert probe.last.credential == "sk-through-postgres-0000"


async def test_the_hint_is_stored_not_derived_on_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fixture: Fixture,
) -> None:
    """Rendering the Models list must never need the master key."""
    service = CatalogService(
        PostgresCatalogStore(db_session_factory),
        secret_box=SecretBox(master_key=os.urandom(32)),
        probe=FakeProbe(),
    )
    actor = Actor(
        user_id=uuid7(),
        scope=TenantScope(role="org_admin", organization_id=fixture.acme.id),
    )

    await service.update_model(
        actor, fixture.acme_model.id, ModelPatch(credential="sk-abcdefgh12345678")
    )

    stored = await db_session.get(UpstreamModel, fixture.acme_model.id)
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.credential_hint == "sk-...5678"
