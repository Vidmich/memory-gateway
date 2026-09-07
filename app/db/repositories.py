"""Org-scoped repositories.

Every table that carries ``organization_id`` gets one, and every query about that table
goes through it. Tasks 05 onward add a class here per aggregate — models, connectors,
gateways, logs — and inherit isolation rather than re-deriving it.

Nothing here takes an organization as an argument. The scope arrives in the constructor,
from the session, which is the property that makes a cross-tenant read a deliberate act
rather than a forgotten ``WHERE``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantScope
from app.db.models import Invitation, Organization, User
from app.db.scoping import ScopedRepository, unscoped


class UserRepository(ScopedRepository[User]):
    """Members of the organization in scope.

    A superadmin has ``organization_id IS NULL``, so a platform account never appears in
    an organization's member list — the scope clause excludes it with no special case.
    """

    model = User

    def page(self, *, after: uuid.UUID | None, limit: int) -> Select[tuple[User]]:
        statement = self.select().order_by(User.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(User.id < after)
        return statement

    async def active_admins(self) -> Sequence[User]:
        """Backs the last-admin guard, and counts *active* admins only: an organization
        whose only admin is suspended has nobody who can appoint another."""
        return await self.fetch(
            self.select().where(User.role == "org_admin", User.status == "active")
        )

    async def by_email_anywhere(self, email: str) -> User | None:
        """Deliberately outside the scope.

        ``users.email`` is globally unique, so creating a member has to check the whole
        table or the insert fails on a constraint the caller cannot see. It returns only
        whether the address is taken — never the row — so an admin cannot use it to read
        another organization's member.
        """
        statement = (
            select(User)
            .where(User.email == email.strip())
            .execution_options(
                **unscoped("email is globally unique; existence is checked, not disclosed")
            )
        )
        return (await self._session.execute(statement)).scalars().first()


class InvitationRepository(ScopedRepository[Invitation]):
    model = Invitation

    def page(self, *, after: uuid.UUID | None, limit: int) -> Select[tuple[Invitation]]:
        statement = self.select().order_by(Invitation.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(Invitation.id < after)
        return statement

    async def pending_for(self, email: str) -> Invitation | None:
        statement = self.select().where(
            Invitation.email == email.strip(),
            Invitation.accepted_at.is_(None),
        )
        return (await self._session.execute(statement)).scalars().first()

    async def by_token_hash(self, token_hash: str) -> Invitation | None:
        """Accepting an invitation is unauthenticated, so there is no scope yet: the
        token *is* the claim, and the row it finds is what establishes the tenant."""
        statement = (
            select(Invitation)
            .where(Invitation.token_hash == token_hash)
            .execution_options(**unscoped("invitation acceptance establishes the tenant"))
        )
        return (await self._session.execute(statement)).scalars().first()


class OrganizationRepository:
    """The one table whose tenant key is its own primary key.

    It cannot inherit :class:`ScopedRepository` — there is no ``organization_id`` column
    to filter on — so the scope is applied to ``id``, in the single method every read
    starts from.
    """

    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    def select(self) -> Select[tuple[Organization]]:
        return select(Organization).where(self._scope.clause_on(Organization.id))

    async def by_id(self, organization_id: uuid.UUID) -> Organization | None:
        statement = self.select().where(Organization.id == organization_id)
        return (await self._session.execute(statement)).scalars().first()

    async def by_slug(self, slug: str) -> Organization | None:
        statement = self.select().where(Organization.slug == slug)
        return (await self._session.execute(statement)).scalars().first()

    async def slug_taken(self, slug: str, *, excluding: uuid.UUID | None = None) -> bool:
        """Outside the scope, like the email check: the slug is globally unique because
        it appears in the public gateway URL, so uniqueness has to be checked globally.
        Returns a boolean, never a row."""
        statement = (
            select(Organization.id)
            .where(Organization.slug == slug)
            .execution_options(**unscoped("organization slugs are globally unique; existence only"))
        )
        if excluding is not None:
            statement = statement.where(Organization.id != excluding)
        return (await self._session.execute(statement)).first() is not None

    async def page(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Organization]:
        statement = self.select().order_by(Organization.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(Organization.id < after)
        return (await self._session.execute(statement)).scalars().all()

    async def add(self, organization: Organization) -> Organization:
        self._session.add(organization)
        await self._session.flush()
        return organization

    async def counts(
        self, model: type[Any], organization_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """One grouped count for a list screen, rather than a query per row.

        Unscoped because it is built only for ids the caller has already resolved through
        a scoped read, and because it selects no tenant data — only how many rows exist.
        """
        if not organization_ids:
            return {}
        statement = (
            select(model.organization_id, func.count())
            .where(model.organization_id.in_(organization_ids))
            .group_by(model.organization_id)
            .execution_options(**unscoped("aggregate over ids the caller already resolved"))
        )
        rows = (await self._session.execute(statement)).all()
        return {row[0]: row[1] for row in rows if row[0] is not None}
