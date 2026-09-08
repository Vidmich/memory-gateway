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
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.tenancy import TenantScope
from app.db.models import (
    ApiKey,
    AuditEvent,
    Connector,
    Document,
    Gateway,
    GatewayTarget,
    Invitation,
    Organization,
    UpstreamModel,
    User,
)
from app.db.scoping import ScopedRepository, scoped, unscoped

if TYPE_CHECKING:  # pragma: no cover - typing only
    # The filter shape belongs to the service that owns the screen, and that module
    # imports this one. Under `from __future__ import annotations` the reference is a
    # string, so the cycle never exists at runtime — the same arrangement
    # `app.core.tenancy` uses for `Identity`.
    from app.services.audit_store import AuditFilters


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


class AuditEventRepository(ScopedRepository[AuditEvent]):
    """The audit trail, read-only.

    Read-only is not a comment: this class inherits :meth:`ScopedRepository.add` and
    :meth:`~app.db.scoping.ScopedRepository.delete` and overrides both to refuse, because
    an event is written into the transaction that is making the change it records — see
    :mod:`app.services.audit` — and never through a repository somebody could reach for
    later. The database refuses an ``UPDATE`` or a ``DELETE`` on this table outright.

    ``organization_id`` is nullable here, unlike every other table with a scope. A
    platform event — a global model created, a platform setting changed — belongs to no
    customer, and the scope clause excludes ``NULL`` for an organization scope, which is
    exactly right: it is the platform's own record and appears in nobody's log but the
    platform's.
    """

    model = AuditEvent

    def page(
        self,
        filters: AuditFilters,
        *,
        after: uuid.UUID | None,
        limit: int,
    ) -> Select[tuple[AuditEvent]]:
        statement = self.select().order_by(AuditEvent.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(AuditEvent.id < after)
        # Narrowing only. For a platform caller this picks one customer out of the whole
        # log; for anybody else it intersects with a scope clause that is already theirs,
        # so an id from another organization returns nothing rather than something.
        if filters.organization_id is not None:
            statement = statement.where(AuditEvent.organization_id == filters.organization_id)
        if filters.actor_user_id is not None:
            statement = statement.where(AuditEvent.actor_user_id == filters.actor_user_id)
        if filters.action is not None:
            statement = statement.where(AuditEvent.action == filters.action)
        if filters.target_type is not None:
            statement = statement.where(AuditEvent.target_type == filters.target_type)
        if filters.target_id is not None:
            statement = statement.where(AuditEvent.target_id == filters.target_id)
        if filters.start is not None:
            statement = statement.where(AuditEvent.created_at >= filters.start)
        if filters.end is not None:
            statement = statement.where(AuditEvent.created_at < filters.end)
        return statement

    async def add(self, entity: AuditEvent) -> AuditEvent:
        raise NotImplementedError(_APPEND_ONLY)

    async def delete(self, entity: AuditEvent) -> None:
        raise NotImplementedError(_APPEND_ONLY)


#: Why the two inherited write methods refuse. One string, so the two answers agree.
_APPEND_ONLY = (
    "audit_events is append-only and is written through the transaction performing the "
    "mutation; see app.services.audit.AuditRecorder."
)


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


# ---------------------------------------------------------------------------
# the model catalog
# ---------------------------------------------------------------------------
#
# `upstream_models` is the first table with **two** scopes: a row either belongs to one
# organization or to the platform, and every org user can read the platform's rows
# (SPEC §5.3, the global catalog). So the read is deliberately wider than
# `ScopedRepository.select()`, and the widening is written once, here, as a matched pair:
# a SQL clause and the row-level predicate that must mean the same thing. They are three
# lines apart for the same reason `TenantScope.clause` and `.permits` are — the memory
# store in `app/services/catalog_store.py` uses the second, and a contract test runs the
# same assertions through both.
#
# What the widening adds is `organization_id IS NULL`, which is platform-owned. It can
# never reach another tenant's row, and `test_cross_tenant.py` holds that.


def visible_models_clause(scope: TenantScope) -> ColumnElement[bool]:
    """``WHERE`` for "models this scope may see": its own, plus the usable catalog.

    A *disabled* global model is not offered to tenants. It cannot serve a request, so
    listing it would only invite somebody to point a gateway at something switched off.
    The platform sees it either way, because the platform is who switches it back on.
    """
    shared: ColumnElement[bool] = UpstreamModel.organization_id.is_(None)
    if not scope.is_platform:
        shared = and_(shared, UpstreamModel.enabled.is_(True))
    return or_(scope.clause(UpstreamModel), shared)


def model_is_visible(
    scope: TenantScope, organization_id: uuid.UUID | None, *, enabled: bool
) -> bool:
    """The same question about a row already in hand."""
    if organization_id is None:
        return scope.is_platform or enabled
    return scope.permits(organization_id)


class UpstreamModelRepository(ScopedRepository[UpstreamModel]):
    """Reads are dual-scope; writes are not.

    Every method that changes a row starts from :meth:`ScopedRepository.select`, so an
    org user editing a global model finds nothing and gets a 404 — the same answer as for
    a model that does not exist. Only :meth:`visible` and the reads built on it widen,
    and they are read-only by construction.
    """

    model = UpstreamModel

    def visible(self) -> Select[tuple[UpstreamModel]]:
        return (
            select(UpstreamModel)
            .where(visible_models_clause(self._scope))
            .execution_options(**scoped())
        )

    async def get_visible(self, model_id: uuid.UUID) -> UpstreamModel | None:
        """For reading one model, including a global one. Editing uses ``get``."""
        statement = self.visible().where(UpstreamModel.id == model_id)
        return (await self._session.execute(statement)).scalars().first()

    def page(
        self,
        *,
        after: uuid.UUID | None,
        limit: int,
        scope_filter: str | None = None,
        enabled: bool | None = None,
    ) -> Select[tuple[UpstreamModel]]:
        statement = self.visible().order_by(UpstreamModel.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(UpstreamModel.id < after)
        if scope_filter is not None:
            statement = statement.where(UpstreamModel.scope == scope_filter)
        if enabled is not None:
            statement = statement.where(UpstreamModel.enabled.is_(enabled))
        return statement

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        """Within this scope only.

        Two organizations may both call a model "gpt-4o", and the unique constraint is
        per organization. The global catalog has its own partial index, and a superadmin
        creating a global model is at platform scope, where this reads the global rows.
        """
        statement = self.select().where(UpstreamModel.name == name.strip())
        if excluding is not None:
            statement = statement.where(UpstreamModel.id != excluding)
        return (await self._session.execute(statement)).scalars().first() is not None

    async def global_name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        """Only the global catalog, whatever the scope.

        A superadmin who has assumed an organization still creates global models into one
        namespace, so the check cannot ride on the scope.
        """
        statement = (
            select(UpstreamModel.id)
            .where(
                UpstreamModel.name == name.strip(),
                UpstreamModel.organization_id.is_(None),
            )
            .execution_options(**unscoped("global model names are one namespace; existence only"))
        )
        if excluding is not None:
            statement = statement.where(UpstreamModel.id != excluding)
        return (await self._session.execute(statement)).first() is not None

    async def add_global(self, model: UpstreamModel) -> UpstreamModel:
        """Insert a platform-owned row.

        Separate from :meth:`ScopedRepository.add`, which stamps the scope's organization
        onto the row and would therefore refuse this one. Named rather than a flag so
        "which call sites create rows nobody owns" is a grep.
        """
        model.organization_id = None
        model.scope = "global"
        self._session.add(model)
        await self._session.flush()
        return model


class GatewayRepository(ScopedRepository[Gateway]):
    """The organization's published endpoints."""

    model = Gateway

    def page(self, *, after: uuid.UUID | None, limit: int) -> Select[tuple[Gateway]]:
        statement = self.with_targets().order_by(Gateway.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(Gateway.id < after)
        return statement

    async def fetch_unique(self, statement: Select[tuple[Gateway]]) -> Sequence[Gateway]:
        """``unique()`` is not optional here: ``joinedload`` on a collection returns one
        row per target, and SQLAlchemy refuses to guess which ones to collapse."""
        return (await self._session.execute(statement)).unique().scalars().all()

    def with_targets(self) -> Select[tuple[Gateway]]:
        """The scoped read with the routing chain and its models already loaded.

        ``selectinload`` rather than a lazy relationship: the list screen renders the
        target model's name for every row, and a lazy load there is one query per gateway
        against a session that may already be closed.
        """
        return self.select().options(
            selectinload(Gateway.targets).joinedload(GatewayTarget.upstream_model)
        )

    async def get_with_targets(self, gateway_id: uuid.UUID) -> Gateway | None:
        statement = self.with_targets().where(Gateway.id == gateway_id)
        return (await self._session.execute(statement)).unique().scalars().first()

    async def slug_taken(self, slug: str) -> bool:
        """Across every organization, because the slug is a public URL segment.

        Like the organization slug and the user email before it: this returns a boolean,
        never a row, so it cannot be used to read another tenant's gateway.
        """
        statement = (
            select(Gateway.id)
            .where(Gateway.slug == slug.strip())
            .execution_options(**unscoped("gateway slugs are globally unique; existence only"))
        )
        return (await self._session.execute(statement)).first() is not None

    async def slugs_referencing(self, model_id: uuid.UUID) -> Sequence[str]:
        """Every gateway slug pointing at a model, ignoring the scope.

        For cache invalidation only, and unscoped because a *global* model is referenced
        from organizations the writer cannot see — leaving their caches stale would be
        the bug this exists to prevent. It selects one column that is already public in
        the URL of an endpoint the reader is editing the target of.
        """
        statement = (
            select(Gateway.slug)
            .join(GatewayTarget, GatewayTarget.gateway_id == Gateway.id)
            .where(GatewayTarget.upstream_model_id == model_id)
            .execution_options(**unscoped("config-cache invalidation spans organizations"))
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def referencing(self, model_id: uuid.UUID) -> Sequence[Gateway]:
        """Gateways in scope with a target on this model.

        Scoped, and that is right in both directions. An org user deleting their own
        model can only be blocked by their own gateways, because SPEC §5.3 forbids a
        gateway referencing another organization's model. A superadmin deleting a global
        model is at platform scope, where the clause is ``true()`` and every referencing
        gateway is found — which is the case that matters, since a global model is the
        one that can be referenced from anywhere.
        """
        statement = (
            self.select()
            .join(GatewayTarget, GatewayTarget.gateway_id == Gateway.id)
            .where(GatewayTarget.upstream_model_id == model_id)
            .order_by(Gateway.slug)
        )
        return (await self._session.execute(statement)).scalars().unique().all()


class ApiKeyRepository:
    """Data-plane keys, scoped through the gateway that owns them.

    ``api_keys`` has no ``organization_id``, which means two things worth stating. The
    tenant key is one join away — a key belongs to a gateway, and the gateway belongs to
    an organization — and, more importantly, **the scope guard cannot help here**:
    :func:`app.db.scoping.is_tenant_keyed` derives its list from the presence of that
    column, so a bare ``select(ApiKey)`` would sail past it. Every read in this class
    therefore joins ``gateways`` and applies the scope clause by hand, and every write
    goes through a gateway the caller has already resolved through a scoped read.

    Denormalising ``organization_id`` onto the table would let the guard cover it, at the
    cost of a column that can disagree with the join. The join is the truth; this class is
    the single place that has to get it right.
    """

    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    def select(self) -> Select[tuple[ApiKey]]:
        return (
            select(ApiKey)
            .join(Gateway, Gateway.id == ApiKey.gateway_id)
            .where(self._scope.clause(Gateway))
            .execution_options(**scoped())
        )

    async def get(self, key_id: uuid.UUID) -> ApiKey | None:
        statement = self.select().where(ApiKey.id == key_id)
        return (await self._session.execute(statement)).scalars().first()

    async def for_gateway(self, gateway_id: uuid.UUID) -> Sequence[ApiKey]:
        """Newest first. Revoked keys are included: they are history, and the screen says
        so — hiding them would make "why is this key not working" unanswerable."""
        statement = self.select().where(ApiKey.gateway_id == gateway_id).order_by(ApiKey.id.desc())
        return (await self._session.execute(statement)).scalars().all()

    async def counts(self, gateway_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Active keys per gateway, for the list screen. One query, not one per row."""
        if not gateway_ids:
            return {}
        statement = (
            select(ApiKey.gateway_id, func.count())
            .where(ApiKey.gateway_id.in_(gateway_ids), ApiKey.revoked_at.is_(None))
            .group_by(ApiKey.gateway_id)
            .execution_options(**unscoped("aggregate over gateway ids already resolved in scope"))
        )
        rows = (await self._session.execute(statement)).all()
        return {row[0]: row[1] for row in rows}

    async def add(self, key: ApiKey) -> ApiKey:
        self._session.add(key)
        await self._session.flush()
        return key


class ConnectorRepository(ScopedRepository[Connector]):
    """Content sources. Ordinary in every way, which is the point of the base class."""

    model = Connector

    def page(self, *, after: uuid.UUID | None, limit: int) -> Select[tuple[Connector]]:
        statement = self.select().order_by(Connector.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(Connector.id < after)
        return statement

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        statement = self.select().where(Connector.name == name.strip())
        if excluding is not None:
            statement = statement.where(Connector.id != excluding)
        return (await self._session.execute(statement)).first() is not None


#: ``(id, source_uri, etag, status)`` — the four columns resync reconciles on.
type DocumentIndexRow = tuple[uuid.UUID, str, str | None, str]


class DocumentRepository(ScopedRepository[Document]):
    """Ingested objects.

    Every read here is scoped by ``organization_id`` *and* narrowed by ``connector_id``.
    The second is not a scoping mechanism — the caller has already resolved the connector
    through a scoped read — it is what keeps a connector's document table from being a
    scan of every document in the organization.
    """

    model = Document

    def page(
        self,
        connector_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        status: str | None = None,
    ) -> Select[tuple[Document]]:
        statement = (
            self.select()
            .where(Document.connector_id == connector_id)
            .order_by(Document.id.desc())
            .limit(limit + 1)
        )
        if status is not None:
            statement = statement.where(Document.status == status)
        if after is not None:
            statement = statement.where(Document.id < after)
        return statement

    async def by_source(self, connector_id: uuid.UUID, source_uri: str) -> Document | None:
        """The reconciliation lookup, matching ``uq_documents_connector_id_source_uri``."""
        statement = self.select().where(
            Document.connector_id == connector_id, Document.source_uri == source_uri
        )
        return (await self._session.execute(statement)).scalars().first()

    async def index(self, connector_id: uuid.UUID) -> Sequence[DocumentIndexRow]:
        """``(id, source_uri, etag, status)`` for every document, for resync.

        Four columns rather than whole rows: a connector with fifty thousand documents is
        reconciled in one pass, and loading fifty thousand ORM objects to compare two
        strings each is how that turns into a memory incident.
        """
        statement = (
            select(Document.id, Document.source_uri, Document.etag, Document.status)
            .where(
                self._scope.clause(Document),
                Document.connector_id == connector_id,
            )
            .execution_options(**scoped())
        )
        rows = (await self._session.execute(statement)).all()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    async def status_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, int]]:
        """Documents per status per connector, for the list screen. One query for the
        whole page, not one per row."""
        if not connector_ids:
            return {}
        statement = (
            select(Document.connector_id, Document.status, func.count())
            .where(self._scope.clause(Document), Document.connector_id.in_(connector_ids))
            .group_by(Document.connector_id, Document.status)
            .execution_options(**scoped())
        )
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for connector_id, status, total in (await self._session.execute(statement)).all():
            counts.setdefault(connector_id, {})[str(status)] = int(total)
        return counts

    async def total_bytes(self, connector_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not connector_ids:
            return {}
        statement = (
            select(Document.connector_id, func.coalesce(func.sum(Document.size_bytes), 0))
            .where(self._scope.clause(Document), Document.connector_id.in_(connector_ids))
            .group_by(Document.connector_id)
            .execution_options(**scoped())
        )
        rows = (await self._session.execute(statement)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def organization_bytes(self) -> int:
        """Everything this organization is storing, for the quota check.

        Reads ``documents`` rather than the object store: a listing of every object under
        every prefix is O(objects) network calls on the request path, and the document
        rows are written from the same bytes.
        """
        statement = (
            select(func.coalesce(func.sum(Document.size_bytes), 0))
            .where(self._scope.clause(Document))
            .execution_options(**scoped())
        )
        return int((await self._session.execute(statement)).scalar() or 0)
