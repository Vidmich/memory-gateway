"""Tenancy against a real PostgreSQL.

Three things that cannot be checked anywhere else: the store contract against the
implementation that ships, the constraints (a CHECK or a partial unique index is only
real if the server enforces it), and the scope guard actually firing at execution rather
than only at statement construction.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFound
from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Invitation, Organization, User
from app.db.scoping import UnscopedQuery, unscoped
from app.services.directory import Actor, DirectoryService
from app.services.directory_store import PostgresDirectoryStore
from tests.directory_store_contract import CHECKS, Check, Fixture
from tests.test_directory_store_memory import HASHER, build_fixture

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
    for user in database.users.values():
        db_session.add(user)
    await db_session.flush()
    for invitation in database.invitations.values():
        db_session.add(invitation)
    await db_session.flush()

    yield Fixture(
        store=PostgresDirectoryStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_admin=memory.acme_admin,
        acme_viewer=memory.acme_viewer,
        globex_admin=memory.globex_admin,
        superadmin=memory.superadmin,
        acme_invitation=memory.acme_invitation,
        globex_invitation=memory.globex_invitation,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    """The same list the in-memory store passes. Neither gets a shorter exam."""
    await check(fixture)


# ---------------------------------------------------------------------------
# the service, end to end
# ---------------------------------------------------------------------------


async def test_the_invitation_lifecycle_against_postgres(fixture: Fixture) -> None:
    service = DirectoryService(fixture.store, hasher=HASHER)
    actor = Actor(user_id=fixture.acme_admin.id, scope=fixture.acme_scope)

    issued = await service.invite(
        actor, fixture.acme.id, email="joiner@example.com", role="org_member"
    )
    assert (await service.preview_invitation(issued.token)).organization_name == "Acme"

    user = await service.accept_invitation(
        issued.token, name="Joiner", password="a-perfectly-fine-password"
    )
    assert user.organization_id == fixture.acme.id

    with pytest.raises(NotFound):
        await service.accept_invitation(
            issued.token, name="Again", password="a-perfectly-fine-password"
        )


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------


async def test_an_invitation_role_must_be_invitable(db_session: AsyncSession) -> None:
    """A superadmin belongs to the platform, so there is no organization to invite them
    into — and the database says so, not only the schema layer."""
    organization = Organization(id=uuid7(), name="Check", slug="check-roles", status="active")
    db_session.add(organization)
    await db_session.flush()

    db_session.add(
        Invitation(
            id=uuid7(),
            organization_id=organization.id,
            email="root@example.com",
            role="superadmin",
            token_hash="0" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_pending_invitation_per_address(db_session: AsyncSession) -> None:
    organization = Organization(id=uuid7(), name="Once", slug="once", status="active")
    db_session.add(organization)
    await db_session.flush()

    for _ in range(2):
        db_session.add(
            Invitation(
                id=uuid7(),
                organization_id=organization.id,
                email="twice@example.com",
                role="org_member",
                token_hash=uuid7().hex + uuid7().hex,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_accepted_invitation_frees_the_address(db_session: AsyncSession) -> None:
    """The uniqueness index is partial: someone who left and came back can be invited
    again, and an accepted row must not block that forever."""
    organization = Organization(id=uuid7(), name="Again", slug="again", status="active")
    db_session.add(organization)
    await db_session.flush()

    db_session.add(
        Invitation(
            id=uuid7(),
            organization_id=organization.id,
            email="returning@example.com",
            role="org_member",
            token_hash=uuid7().hex + uuid7().hex,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            accepted_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    db_session.add(
        Invitation(
            id=uuid7(),
            organization_id=organization.id,
            email="returning@example.com",
            role="org_member",
            token_hash=uuid7().hex + uuid7().hex,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.flush()  # no IntegrityError


async def test_the_email_is_case_insensitive_within_an_invitation(
    db_session: AsyncSession,
) -> None:
    """CITEXT, so the pending-invitation index catches a differently-cased retry."""
    organization = Organization(id=uuid7(), name="Case", slug="case", status="active")
    db_session.add(organization)
    await db_session.flush()

    for address in ("Person@Example.com", "person@example.com"):
        db_session.add(
            Invitation(
                id=uuid7(),
                organization_id=organization.id,
                email=address,
                role="org_member",
                token_hash=uuid7().hex + uuid7().hex,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_organization_settings_default_to_an_empty_object(
    db_session: AsyncSession,
) -> None:
    """NOT NULL with a server default, so task 07 and 13 can read it without handling
    ``None``."""
    organization = Organization(id=uuid7(), name="Defaults", slug="defaults")
    db_session.add(organization)
    await db_session.flush()
    await db_session.refresh(organization)

    assert organization.settings == {}
    assert organization.status == "active"


async def test_deleting_an_organization_takes_its_invitations(
    db_session: AsyncSession,
) -> None:
    organization = Organization(id=uuid7(), name="Gone", slug="gone", status="active")
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        Invitation(
            id=uuid7(),
            organization_id=organization.id,
            email="orphan@example.com",
            role="org_member",
            token_hash=uuid7().hex + uuid7().hex,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.flush()

    await db_session.delete(organization)
    await db_session.flush()

    remaining = await db_session.execute(
        select(Invitation)
        .where(Invitation.organization_id == organization.id)
        .execution_options(**unscoped("test: verifying the cascade removed everything"))
    )
    assert remaining.scalars().all() == []


# ---------------------------------------------------------------------------
# the guard, at execution
# ---------------------------------------------------------------------------


async def test_the_scope_guard_fires_on_a_live_session(db_session: AsyncSession) -> None:
    """Everywhere else this is asserted against a connection that does not exist. Here it
    is asserted against one that does, which is the case that matters."""
    with pytest.raises(UnscopedQuery):
        await db_session.execute(select(User))


async def test_a_declared_bypass_still_works_on_a_live_session(
    db_session: AsyncSession,
) -> None:
    result = await db_session.execute(
        select(User).execution_options(**unscoped("test: the bypass has to actually run"))
    )
    assert result.scalars().all() is not None


async def test_a_scoped_read_returns_only_the_scoped_rows(fixture: Fixture) -> None:
    """The end-to-end version of the whole task: a real query, a real database, and the
    other organization's rows are simply not in the answer."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        members = await transaction.members(after=None, limit=100)

    assert {member.id for member in members} == {
        fixture.acme_admin.id,
        fixture.acme_viewer.id,
    }


async def test_a_narrowed_platform_scope_sees_one_organization(fixture: Fixture) -> None:
    platform = TenantScope(role="superadmin", organization_id=None)
    async with fixture.store.begin(platform) as transaction:
        inner = transaction.narrowed(fixture.globex_scope)
        members = await inner.members(after=None, limit=100)

    assert [member.id for member in members] == [fixture.globex_admin.id]
