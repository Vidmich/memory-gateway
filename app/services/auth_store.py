"""Persistence for control-plane auth, behind a port.

Two reasons for the indirection, in order of importance.

**The rotation state machine is the security-critical part of this feature and it is not
about SQL.** "A replaced token presented twice revokes the family" is a rule about
ordering and state, and it deserves tests that run everywhere, on every commit, without a
database container. Those tests use :class:`MemoryAuthStore`, and they are the ones that
would catch a regression in the rule.

**SPEC §5.3 asks for tenant scoping to be enforced in a repository rather than in each
endpoint.** Task 04 needs somewhere to put that; this is the shape it goes in.

:class:`PostgresAuthStore` is the real one. It is thin on purpose — every method is one
statement — and a ``db``-marked contract test runs the same assertions against both
implementations, so the in-memory one cannot quietly drift into a more convenient
behaviour than the database actually has.

Transactions are explicit: the service calls :meth:`AuthTransaction.commit` itself,
because several paths must *persist a revocation and then raise*. An implicit
commit-on-clean-exit would throw that work away exactly when it matters most.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Organization, User, UserSession


class AuthTransaction(Protocol):
    """One unit of work. Objects it returns are live: mutating them and committing
    persists the change, in both implementations."""

    async def user_by_email(self, email: str) -> User | None: ...

    async def user_by_id(self, user_id: uuid.UUID) -> User | None: ...

    async def organization(self, organization_id: uuid.UUID) -> Organization | None: ...

    async def add_session(self, record: UserSession) -> None: ...

    async def session_by_token_hash(self, token_hash: str) -> UserSession | None: ...

    async def family_is_live(self, family_id: uuid.UUID) -> bool: ...

    async def revoke_family(self, family_id: uuid.UUID, *, reason: str, at: datetime) -> None: ...

    async def revoke_other_families(
        self,
        *,
        user_id: uuid.UUID,
        keep_family_id: uuid.UUID,
        reason: str,
        at: datetime,
    ) -> None: ...

    async def commit(self) -> None: ...


class AuthStore(Protocol):
    def begin(self) -> AbstractAsyncContextManager[AuthTransaction]:
        """Open a transaction. Used as ``async with store.begin() as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresAuthTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_by_email(self, email: str) -> User | None:
        # No ``lower()``: the column is CITEXT, so the comparison is already
        # case-insensitive *and* can still use the unique index.
        statement = select(User).where(User.email == email.strip())
        return (await self._session.execute(statement)).scalars().first()

    async def user_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def organization(self, organization_id: uuid.UUID) -> Organization | None:
        return await self._session.get(Organization, organization_id)

    async def add_session(self, record: UserSession) -> None:
        self._session.add(record)
        await self._session.flush()

    async def session_by_token_hash(self, token_hash: str) -> UserSession | None:
        statement = select(UserSession).where(UserSession.refresh_token_hash == token_hash)
        return (await self._session.execute(statement)).scalars().first()

    async def family_is_live(self, family_id: uuid.UUID) -> bool:
        statement = (
            select(UserSession.id)
            .where(UserSession.family_id == family_id, UserSession.revoked_at.is_(None))
            .limit(1)
        )
        return (await self._session.execute(statement)).first() is not None

    async def revoke_family(self, family_id: uuid.UUID, *, reason: str, at: datetime) -> None:
        await self._session.execute(
            update(UserSession)
            .where(UserSession.family_id == family_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=at, revoked_reason=reason)
            # Rows already loaded in this session would otherwise keep their stale
            # `revoked_at`, and the service reads one of them straight afterwards.
            .execution_options(synchronize_session="fetch")
        )

    async def revoke_other_families(
        self,
        *,
        user_id: uuid.UUID,
        keep_family_id: uuid.UUID,
        reason: str,
        at: datetime,
    ) -> None:
        await self._session.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.family_id != keep_family_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=at, revoked_reason=reason)
            .execution_options(synchronize_session="fetch")
        )

    async def commit(self) -> None:
        await self._session.commit()


class PostgresAuthStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AuthTransaction]:
        # No commit on exit: leaving the context without one rolls back, which is what
        # should happen to a half-finished login.
        async with self._session_factory() as session:
            yield PostgresAuthTransaction(session)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryAuthTransaction:
    """Backed by plain dictionaries.

    ``commit`` is a no-op and there is no rollback: writes are visible the moment they
    happen. Nothing in the auth flows depends on rolling one back, and pretending
    otherwise would be more misleading than saying so here.
    """

    def __init__(self, state: _MemoryState) -> None:
        self._state = state

    async def user_by_email(self, email: str) -> User | None:
        wanted = email.strip().casefold()
        for user in self._state.users.values():
            if user.email.casefold() == wanted:
                return user
        return None

    async def user_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._state.users.get(user_id)

    async def organization(self, organization_id: uuid.UUID) -> Organization | None:
        return self._state.organizations.get(organization_id)

    async def add_session(self, record: UserSession) -> None:
        self._state.sessions[record.id] = record

    async def session_by_token_hash(self, token_hash: str) -> UserSession | None:
        for record in self._state.sessions.values():
            if record.refresh_token_hash == token_hash:
                return record
        return None

    async def family_is_live(self, family_id: uuid.UUID) -> bool:
        return any(
            record.family_id == family_id and record.revoked_at is None
            for record in self._state.sessions.values()
        )

    async def revoke_family(self, family_id: uuid.UUID, *, reason: str, at: datetime) -> None:
        for record in self._state.sessions.values():
            if record.family_id == family_id and record.revoked_at is None:
                record.revoked_at = at
                record.revoked_reason = reason

    async def revoke_other_families(
        self,
        *,
        user_id: uuid.UUID,
        keep_family_id: uuid.UUID,
        reason: str,
        at: datetime,
    ) -> None:
        for record in self._state.sessions.values():
            if (
                record.user_id == user_id
                and record.family_id != keep_family_id
                and record.revoked_at is None
            ):
                record.revoked_at = at
                record.revoked_reason = reason

    async def commit(self) -> None:
        return None


class _MemoryState:
    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}
        self.organizations: dict[uuid.UUID, Organization] = {}
        self.sessions: dict[uuid.UUID, UserSession] = {}


class MemoryAuthStore:
    """For tests, and for the contract test that keeps it honest."""

    def __init__(self) -> None:
        self._state = _MemoryState()

    def add_user(self, user: User) -> User:
        self._state.users[user.id] = user
        return user

    def add_organization(self, organization: Organization) -> Organization:
        self._state.organizations[organization.id] = organization
        return organization

    @property
    def sessions(self) -> dict[uuid.UUID, UserSession]:
        return self._state.sessions

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AuthTransaction]:
        yield MemoryAuthTransaction(self._state)
