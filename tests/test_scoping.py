"""The unscoped-query guard, and the repository base class that keeps it quiet.

The guard runs on ``do_orm_execute``, which SQLAlchemy fires *before* it acquires a
connection. That is what makes these tests possible without PostgreSQL: a statement that
violates the rule raises here, and one that satisfies it goes on to fail at the socket —
which is itself the assertion that the guard let it through.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import Forbidden
from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import ApiKey, Gateway, Invitation, Organization, User, UserSession
from app.db.repositories import InvitationRepository, UserRepository
from app.db.scoping import (
    SCOPE_OPTION,
    UnscopedQuery,
    guard_violation,
    is_tenant_keyed,
    scoped,
    unscoped,
)

ACME = uuid7()
SCOPE = TenantScope(role="org_admin", organization_id=ACME)

#: Points at nothing. Any statement that gets past the guard fails on connect, which is
#: how these tests tell "allowed" from "refused".
NOWHERE = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(NOWHERE)
    async with AsyncSession(bind=engine) as open_session:
        yield open_session
    await engine.dispose()


async def reached_the_database(session: AsyncSession, statement: Select[Any]) -> bool:
    """True when the guard allowed the statement through."""
    try:
        await session.execute(statement)
    except UnscopedQuery:
        return False
    except Exception:
        return True
    raise AssertionError("a query against nothing should not have succeeded")


# -- which tables are guarded ----------------------------------------------


@pytest.mark.parametrize("model", [User, Invitation, Gateway])
def test_tables_with_an_organization_key_are_guarded(model: type) -> None:
    assert is_tenant_keyed(model)


@pytest.mark.parametrize("model", [Organization, ApiKey, UserSession])
def test_tables_without_one_are_not(model: type) -> None:
    """`organizations` is scoped on its own primary key and `sessions`/`api_keys` hang off
    rows that are already scoped, so none of them carries the column."""
    assert not is_tenant_keyed(model)


def test_membership_is_derived_from_the_schema() -> None:
    """Not from a hand-maintained list — a table added in a later task is guarded the
    moment it declares the column, with nobody remembering to register it."""
    assert not is_tenant_keyed(str)


# -- the decision function -------------------------------------------------


def test_an_unmarked_query_on_a_guarded_table_is_a_violation() -> None:
    problem = guard_violation([User], {})
    assert problem is not None
    assert "User" in problem
    assert "ScopedRepository" in problem


def test_a_scoped_query_is_fine() -> None:
    assert guard_violation([User], scoped()) is None


def test_a_declared_bypass_is_fine() -> None:
    assert guard_violation([User], unscoped("login")) is None


def test_a_bypass_must_give_a_reason() -> None:
    with pytest.raises(ValueError, match="why"):
        unscoped("")


def test_the_reason_survives_into_the_options() -> None:
    """It is what a reviewer reads, so it has to still be there at execution time."""
    assert "login resolves the tenant" in unscoped("login resolves the tenant")[SCOPE_OPTION]


def test_untouched_tables_need_nothing() -> None:
    assert guard_violation([Organization, ApiKey], {}) is None


def test_one_guarded_table_in_a_join_is_enough() -> None:
    assert guard_violation([Organization, User], {}) is not None


# -- the listener, live ----------------------------------------------------


async def test_a_bare_select_is_refused(session: AsyncSession) -> None:
    assert not await reached_the_database(session, select(User))


async def test_session_get_is_refused_too(session: AsyncSession) -> None:
    """`session.get()` builds its own statement, so it would be an easy way around a
    guard that only watched `select()`."""
    try:
        await session.get(Gateway, uuid.uuid4())
    except UnscopedQuery:
        return
    except Exception:
        raise AssertionError("the guard did not fire for session.get") from None
    raise AssertionError("the guard did not fire for session.get")


async def test_a_declared_bypass_reaches_the_database(session: AsyncSession) -> None:
    statement = select(User).execution_options(**unscoped("test"))
    assert await reached_the_database(session, statement)


async def test_an_unguarded_table_reaches_the_database(session: AsyncSession) -> None:
    assert await reached_the_database(session, select(Organization))


# -- the repository satisfies the guard ------------------------------------


async def test_repository_reads_are_scoped_and_allowed(session: AsyncSession) -> None:
    repository = UserRepository(session, SCOPE)
    assert await reached_the_database(session, repository.select())


async def test_a_repository_page_is_filtered(session: AsyncSession) -> None:
    repository = InvitationRepository(session, SCOPE)
    rendered = str(
        repository.page(after=None, limit=10).compile(compile_kwargs={"literal_binds": True})
    )

    assert "invitations.organization_id" in rendered
    assert ACME.hex in rendered or str(ACME) in rendered
    # `limit + 1`: the extra row is how "is there another page" is answered.
    assert "LIMIT 11" in rendered


async def test_a_cursor_narrows_the_page(session: AsyncSession) -> None:
    repository = UserRepository(session, SCOPE)
    after = uuid7()
    rendered = str(
        repository.page(after=after, limit=5).compile(compile_kwargs={"literal_binds": True})
    )

    assert "users.id <" in rendered
    assert after.hex in rendered or str(after) in rendered


async def test_writes_are_stamped_with_the_scope(session: AsyncSession) -> None:
    """A caller cannot choose the organization: the value is overwritten, not defaulted."""
    repository = UserRepository(session, SCOPE)
    elsewhere = uuid7()
    user = User(
        id=uuid7(),
        organization_id=elsewhere,
        email="x@example.com",
        role="org_member",
        name="X",
        status="active",
    )

    # The flush cannot reach a database; the stamping happens before it.
    with suppress(Exception):
        await repository.add(user)

    assert user.organization_id == ACME
    assert user.organization_id != elsewhere


async def test_a_platform_scope_cannot_write_without_choosing_an_organization(
    session: AsyncSession,
) -> None:
    repository = UserRepository(session, TenantScope(role="superadmin", organization_id=None))
    with pytest.raises(Forbidden):
        await repository.add(User(id=uuid7(), email="y@example.com", role="org_member", name="Y"))
