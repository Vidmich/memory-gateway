"""Persistence for gateways and their keys, behind a port.

Same shape and same reasons as :mod:`app.services.directory_store` and
:mod:`app.services.catalog_store`: what is worth testing here — a slug is unique across
every organization, a revoked key is still listed, a gateway cannot point at another
tenant's model — is a rule about state, and those tests should run on every commit
without a database container.

Two things this port is careful about.

**Keys are scoped through their gateway.** ``api_keys`` carries no ``organization_id``,
so :class:`~app.db.repositories.ApiKeyRepository` joins ``gateways`` for every read. The
memory implementation does the same lookup, in the same order, so a scoping bug shows up
in the contract test rather than only in production.

**A target must be a model the caller can see.** :meth:`GatewayTransaction.visible_model`
is the *wide* catalog read — own models plus the global ones — because SPEC §5.3 lets a
gateway reference either. It is deliberately the same predicate the Models screen uses, so
"you can pick it" and "you can point at it" cannot drift apart.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import ApiKey, Gateway, GatewayTarget, UpstreamModel
from app.db.repositories import (
    ApiKeyRepository,
    GatewayRepository,
    OrganizationRepository,
    UpstreamModelRepository,
    model_is_visible,
)
from app.services.memory_db import MemoryDatabase


class GatewayTransaction(Protocol):
    """One unit of work, already scoped. Returned objects are live in both
    implementations: mutate one and commit."""

    @property
    def scope(self) -> TenantScope: ...

    async def gateway(self, gateway_id: uuid.UUID) -> Gateway | None:
        """Scoped, with targets and their models loaded."""

    async def gateways(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Gateway]: ...

    async def slug_taken(self, slug: str) -> bool:
        """Across every organization: the slug is a public URL segment."""

    async def organization_settings(self) -> Mapping[str, Any]:
        """The scope's organization settings, for the defaults a new gateway inherits.

        Empty for a platform scope, which has no organization to inherit from — and
        cannot create a gateway anyway, since ``add_gateway`` stamps the scope's
        organization onto the row.
        """

    async def visible_model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        """A model this scope may point a gateway at: its own, or the global catalog."""

    async def add_gateway(self, gateway: Gateway) -> Gateway: ...

    async def delete_gateway(self, gateway: Gateway) -> None: ...

    async def set_targets(self, gateway: Gateway, models: Sequence[UpstreamModel]) -> None:
        """Replace the routing chain, in the order given.

        Takes the loaded models rather than their ids so the new rows carry
        ``upstream_model`` already populated. Assigning ids alone would leave the
        relationship unloaded, and reading it afterwards — which the response does —
        is a lazy load on an async session, which raises rather than querying.
        """

    async def keys(self, gateway_id: uuid.UUID) -> Sequence[ApiKey]: ...

    async def key(self, key_id: uuid.UUID) -> ApiKey | None: ...

    async def key_counts(self, gateway_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]: ...

    async def add_key(self, key: ApiKey) -> ApiKey: ...

    async def commit(self) -> None: ...


class GatewayStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[GatewayTransaction]:
        """Open a scoped transaction: ``async with store.begin(scope) as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresGatewayTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._gateways = GatewayRepository(session, scope)
        self._models = UpstreamModelRepository(session, scope)
        self._keys = ApiKeyRepository(session, scope)
        self._organizations = OrganizationRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def gateway(self, gateway_id: uuid.UUID) -> Gateway | None:
        return await self._gateways.get_with_targets(gateway_id)

    async def gateways(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Gateway]:
        return await self._gateways.fetch_unique(self._gateways.page(after=after, limit=limit))

    async def slug_taken(self, slug: str) -> bool:
        return await self._gateways.slug_taken(slug)

    async def organization_settings(self) -> Mapping[str, Any]:
        organization_id = self._scope.organization_id
        if organization_id is None:
            return {}
        organization = await self._organizations.by_id(organization_id)
        return dict(organization.settings or {}) if organization is not None else {}

    async def visible_model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        return await self._models.get_visible(model_id)

    async def add_gateway(self, gateway: Gateway) -> Gateway:
        return await self._gateways.add(gateway)

    async def delete_gateway(self, gateway: Gateway) -> None:
        await self._gateways.delete(gateway)

    async def set_targets(self, gateway: Gateway, models: Sequence[UpstreamModel]) -> None:
        # Replaced wholesale rather than diffed. `cascade="all, delete-orphan"` removes
        # the dropped rows, the list is at most a handful long, and a diff would have to
        # get priority renumbering right for no benefit anyone can see.
        gateway.targets = [
            GatewayTarget(
                id=uuid7(),
                gateway_id=gateway.id,
                upstream_model_id=model.id,
                priority=index,
                upstream_model=model,
            )
            for index, model in enumerate(models)
        ]
        await self._session.flush()

    async def keys(self, gateway_id: uuid.UUID) -> Sequence[ApiKey]:
        return await self._keys.for_gateway(gateway_id)

    async def key(self, key_id: uuid.UUID) -> ApiKey | None:
        return await self._keys.get(key_id)

    async def key_counts(self, gateway_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        return await self._keys.counts(gateway_ids)

    async def add_key(self, key: ApiKey) -> ApiKey:
        return await self._keys.add(key)

    async def commit(self) -> None:
        await self._session.commit()


class PostgresGatewayStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[GatewayTransaction]:
        # No commit on exit: leaving without one rolls back, which is what should happen
        # to a half-applied gateway edit.
        async with self._session_factory() as session:
            yield PostgresGatewayTransaction(session, scope)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryGatewayTransaction:
    """Dictionaries, filtered through the same predicates the SQL clauses are built
    from — :meth:`TenantScope.permits` and :func:`model_is_visible`."""

    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def gateway(self, gateway_id: uuid.UUID) -> Gateway | None:
        found = self._db.gateways.get(gateway_id)
        if found is None or not self._scope.permits(found.organization_id):
            return None
        return self._hydrate(found)

    async def gateways(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Gateway]:
        rows = [
            gateway
            for gateway in self._db.gateways.values()
            if self._scope.permits(gateway.organization_id)
        ]
        # Newest first, matching ORDER BY id DESC — UUIDv7 ids sort by creation time.
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return [self._hydrate(row) for row in ordered[: limit + 1]]

    async def slug_taken(self, slug: str) -> bool:
        wanted = slug.strip()
        return any(gateway.slug == wanted for gateway in self._db.gateways.values())

    async def organization_settings(self) -> Mapping[str, Any]:
        organization_id = self._scope.organization_id
        if organization_id is None:
            return {}
        organization = self._db.organizations.get(organization_id)
        return dict(organization.settings or {}) if organization is not None else {}

    async def visible_model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        found = self._db.upstream_models.get(model_id)
        if found is None or not model_is_visible(
            self._scope, found.organization_id, enabled=found.enabled
        ):
            return None
        return found

    async def add_gateway(self, gateway: Gateway) -> Gateway:
        gateway.organization_id = self._scope.require_organization()
        return self._db.add_gateway(gateway)

    async def delete_gateway(self, gateway: Gateway) -> None:
        self._db.gateways.pop(gateway.id, None)
        for target in list(self._db.gateway_targets.values()):
            if target.gateway_id == gateway.id:
                self._db.gateway_targets.pop(target.id, None)
        # `ON DELETE CASCADE` in the schema; the same thing here, so a test cannot pass
        # against orphaned keys the database would have removed.
        for key in list(self._db.api_keys.values()):
            if key.gateway_id == gateway.id:
                self._db.api_keys.pop(key.id, None)

    async def set_targets(self, gateway: Gateway, models: Sequence[UpstreamModel]) -> None:
        for target in list(self._db.gateway_targets.values()):
            if target.gateway_id == gateway.id:
                self._db.gateway_targets.pop(target.id, None)
        for index, model in enumerate(models):
            self._db.add_target(
                GatewayTarget(
                    id=uuid7(),
                    gateway_id=gateway.id,
                    upstream_model_id=model.id,
                    priority=index,
                    weight=100,
                )
            )
        self._hydrate(gateway)

    async def keys(self, gateway_id: uuid.UUID) -> Sequence[ApiKey]:
        gateway = self._db.gateways.get(gateway_id)
        if gateway is None or not self._scope.permits(gateway.organization_id):
            return []
        rows = [key for key in self._db.api_keys.values() if key.gateway_id == gateway_id]
        return sorted(rows, key=lambda row: row.id, reverse=True)

    async def key(self, key_id: uuid.UUID) -> ApiKey | None:
        found = self._db.api_keys.get(key_id)
        if found is None:
            return None
        gateway = self._db.gateways.get(found.gateway_id)
        # The join, spelled out: a key is inside the scope only if its gateway is.
        if gateway is None or not self._scope.permits(gateway.organization_id):
            return None
        return found

    async def key_counts(self, gateway_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        wanted = set(gateway_ids)
        counts: dict[uuid.UUID, int] = {}
        for key in self._db.api_keys.values():
            if key.gateway_id in wanted and key.revoked_at is None:
                counts[key.gateway_id] = counts.get(key.gateway_id, 0) + 1
        return counts

    async def add_key(self, key: ApiKey) -> ApiKey:
        return self._db.add_key(key)

    async def commit(self) -> None:
        return None

    def _hydrate(self, gateway: Gateway) -> Gateway:
        """Attach the targets and their models, as ``selectinload`` does in Postgres.

        Without this a memory-backed gateway has an empty ``targets`` list and every
        assertion about routing passes for the wrong reason.
        """
        targets = sorted(
            (
                target
                for target in self._db.gateway_targets.values()
                if target.gateway_id == gateway.id
            ),
            key=lambda target: target.priority,
        )
        for target in targets:
            model = self._db.upstream_models.get(target.upstream_model_id)
            if model is not None:
                target.upstream_model = model
        gateway.targets = list(targets)
        return gateway


class MemoryGatewayStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[GatewayTransaction]:
        yield MemoryGatewayTransaction(self._db, scope)


__all__ = [
    "GatewayStore",
    "GatewayTransaction",
    "MemoryGatewayStore",
    "MemoryGatewayTransaction",
    "PostgresGatewayStore",
    "PostgresGatewayTransaction",
]
