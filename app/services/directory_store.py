"""Persistence for organizations, members and invitations, behind a port.

Same shape and same reasons as :mod:`app.services.auth_store`: the rules worth testing
here — cross-tenant reads return nothing, the last admin cannot be removed, an invitation
is single-use — are rules about state, not about SQL, and they deserve tests that run on
every commit without a database container.

:class:`PostgresDirectoryTransaction` is the real one, and it is thin: every method
delegates to a repository in :mod:`app.db.repositories`, so the ``WHERE organization_id``
lives in exactly one place and this module cannot forget it.
:class:`MemoryDirectoryTransaction` answers the same questions from dictionaries using
:meth:`TenantScope.permits` — the same predicate the SQL clause is built from — so the
two cannot disagree about what "in scope" means. A contract test runs both.

Narrowing: a superadmin acting on one organization needs a transaction scoped to it, and
that scope is only known after a row has been read. :meth:`DirectoryTransaction.narrowed`
returns a view over the same unit of work with a different scope, rather than making
every method take an organization argument that some caller will eventually get wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import Gateway, Invitation, Organization, User
from app.db.repositories import InvitationRepository, OrganizationRepository, UserRepository
from app.services.memory_db import MemoryDatabase


class DirectoryTransaction(Protocol):
    """One unit of work, already scoped. Returned objects are live: mutating one and
    committing persists the change, in both implementations."""

    @property
    def scope(self) -> TenantScope: ...

    def narrowed(self, scope: TenantScope) -> DirectoryTransaction: ...

    # -- organizations ----------------------------------------------------

    async def organization(self, organization_id: uuid.UUID) -> Organization | None: ...

    async def organizations(
        self, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[Organization]: ...

    async def add_organization(self, organization: Organization) -> Organization: ...

    async def slug_taken(self, slug: str, *, excluding: uuid.UUID | None = None) -> bool: ...

    async def counts(
        self, model: type[Any], organization_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]: ...

    # -- members ----------------------------------------------------------

    async def member(self, user_id: uuid.UUID) -> User | None: ...

    async def members(self, *, after: uuid.UUID | None, limit: int) -> Sequence[User]: ...

    async def active_admins(self) -> Sequence[User]: ...

    async def email_taken(self, email: str) -> bool: ...

    async def add_user(self, user: User) -> User: ...

    async def delete_user(self, user: User) -> None: ...

    # -- invitations ------------------------------------------------------

    async def invitation(self, invitation_id: uuid.UUID) -> Invitation | None: ...

    async def invitations(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Invitation]: ...

    async def invitation_by_token_hash(self, token_hash: str) -> Invitation | None: ...

    async def pending_invitation_for(self, email: str) -> Invitation | None: ...

    async def add_invitation(self, invitation: Invitation) -> Invitation: ...

    async def delete_invitation(self, invitation: Invitation) -> None: ...

    async def commit(self) -> None: ...


class DirectoryStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[DirectoryTransaction]:
        """Open a scoped transaction: ``async with store.begin(scope) as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresDirectoryTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._users = UserRepository(session, scope)
        self._invitations = InvitationRepository(session, scope)
        self._organizations = OrganizationRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def narrowed(self, scope: TenantScope) -> DirectoryTransaction:
        return PostgresDirectoryTransaction(self._session, scope)

    async def organization(self, organization_id: uuid.UUID) -> Organization | None:
        return await self._organizations.by_id(organization_id)

    async def organizations(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Organization]:
        return await self._organizations.page(after=after, limit=limit)

    async def add_organization(self, organization: Organization) -> Organization:
        return await self._organizations.add(organization)

    async def slug_taken(self, slug: str, *, excluding: uuid.UUID | None = None) -> bool:
        return await self._organizations.slug_taken(slug, excluding=excluding)

    async def counts(
        self, model: type[Any], organization_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        return await self._organizations.counts(model, organization_ids)

    async def member(self, user_id: uuid.UUID) -> User | None:
        return await self._users.get(user_id)

    async def members(self, *, after: uuid.UUID | None, limit: int) -> Sequence[User]:
        return await self._users.fetch(self._users.page(after=after, limit=limit))

    async def active_admins(self) -> Sequence[User]:
        return await self._users.active_admins()

    async def email_taken(self, email: str) -> bool:
        return await self._users.by_email_anywhere(email) is not None

    async def add_user(self, user: User) -> User:
        return await self._users.add(user)

    async def delete_user(self, user: User) -> None:
        await self._users.delete(user)

    async def invitation(self, invitation_id: uuid.UUID) -> Invitation | None:
        return await self._invitations.get(invitation_id)

    async def invitations(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Invitation]:
        return await self._invitations.fetch(self._invitations.page(after=after, limit=limit))

    async def invitation_by_token_hash(self, token_hash: str) -> Invitation | None:
        return await self._invitations.by_token_hash(token_hash)

    async def pending_invitation_for(self, email: str) -> Invitation | None:
        return await self._invitations.pending_for(email)

    async def add_invitation(self, invitation: Invitation) -> Invitation:
        return await self._invitations.add(invitation)

    async def delete_invitation(self, invitation: Invitation) -> None:
        await self._invitations.delete(invitation)

    async def commit(self) -> None:
        await self._session.commit()


class PostgresDirectoryStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[DirectoryTransaction]:
        # No commit on exit: leaving without one rolls back, which is what should happen
        # to a half-applied member change.
        async with self._session_factory() as session:
            yield PostgresDirectoryTransaction(session, scope)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryDirectoryTransaction:
    """Dictionaries, filtered through the same :meth:`TenantScope.permits` predicate the
    SQL clause is built from. ``commit`` is a no-op and writes are visible immediately."""

    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def narrowed(self, scope: TenantScope) -> DirectoryTransaction:
        return MemoryDirectoryTransaction(self._db, scope)

    # -- organizations ----------------------------------------------------

    async def organization(self, organization_id: uuid.UUID) -> Organization | None:
        organization = self._db.organizations.get(organization_id)
        if organization is None or not self._scope.permits(organization.id):
            return None
        return organization

    async def organizations(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Organization]:
        rows = [
            organization
            for organization in self._db.organizations.values()
            if self._scope.permits(organization.id)
        ]
        return _page(rows, after=after, limit=limit)

    async def add_organization(self, organization: Organization) -> Organization:
        return self._db.add_organization(organization)

    async def slug_taken(self, slug: str, *, excluding: uuid.UUID | None = None) -> bool:
        return any(
            organization.slug == slug and organization.id != excluding
            for organization in self._db.organizations.values()
        )

    async def counts(
        self, model: type[Any], organization_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        wanted = set(organization_ids)
        rows: Sequence[Any] = ()
        if model is User:
            rows = list(self._db.users.values())
        elif model is Gateway:
            rows = list(self._db.gateways.values())
        counts: dict[uuid.UUID, int] = {}
        for row in rows:
            organization_id = getattr(row, "organization_id", None)
            if organization_id in wanted:
                counts[organization_id] = counts.get(organization_id, 0) + 1
        return counts

    # -- members ----------------------------------------------------------

    async def member(self, user_id: uuid.UUID) -> User | None:
        user = self._db.users.get(user_id)
        if user is None or not self._scope.permits(user.organization_id):
            return None
        return user

    async def members(self, *, after: uuid.UUID | None, limit: int) -> Sequence[User]:
        rows = [
            user for user in self._db.users.values() if self._scope.permits(user.organization_id)
        ]
        return _page(rows, after=after, limit=limit)

    async def active_admins(self) -> Sequence[User]:
        return [
            user
            for user in self._db.users.values()
            if self._scope.permits(user.organization_id)
            and user.role == "org_admin"
            and user.status == "active"
        ]

    async def email_taken(self, email: str) -> bool:
        wanted = email.strip().casefold()
        return any(user.email.casefold() == wanted for user in self._db.users.values())

    async def add_user(self, user: User) -> User:
        user.organization_id = self._scope.require_organization()
        return self._db.add_user(user)

    async def delete_user(self, user: User) -> None:
        self._db.users.pop(user.id, None)

    # -- invitations ------------------------------------------------------

    async def invitation(self, invitation_id: uuid.UUID) -> Invitation | None:
        invitation = self._db.invitations.get(invitation_id)
        if invitation is None or not self._scope.permits(invitation.organization_id):
            return None
        return invitation

    async def invitations(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Invitation]:
        rows = [
            invitation
            for invitation in self._db.invitations.values()
            if self._scope.permits(invitation.organization_id)
        ]
        return _page(rows, after=after, limit=limit)

    async def invitation_by_token_hash(self, token_hash: str) -> Invitation | None:
        for invitation in self._db.invitations.values():
            if invitation.token_hash == token_hash:
                return invitation
        return None

    async def pending_invitation_for(self, email: str) -> Invitation | None:
        wanted = email.strip().casefold()
        for invitation in self._db.invitations.values():
            if (
                self._scope.permits(invitation.organization_id)
                and invitation.email.casefold() == wanted
                and invitation.accepted_at is None
            ):
                return invitation
        return None

    async def add_invitation(self, invitation: Invitation) -> Invitation:
        invitation.organization_id = self._scope.require_organization()
        return self._db.add_invitation(invitation)

    async def delete_invitation(self, invitation: Invitation) -> None:
        self._db.invitations.pop(invitation.id, None)

    async def commit(self) -> None:
        return None


class MemoryDirectoryStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[DirectoryTransaction]:
        yield MemoryDirectoryTransaction(self._db, scope)


def _page[T: (Organization, User, Invitation)](
    rows: list[T], *, after: uuid.UUID | None, limit: int
) -> Sequence[T]:
    """Newest first, matching ``ORDER BY id DESC`` — UUIDv7 ids sort by creation time."""
    ordered = sorted(rows, key=lambda row: row.id, reverse=True)
    if after is not None:
        ordered = [row for row in ordered if row.id < after]
    return ordered[: limit + 1]
