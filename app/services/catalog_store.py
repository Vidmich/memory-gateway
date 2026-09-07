"""Persistence for the model catalog, behind a port.

Same shape and same reasons as :mod:`app.services.directory_store`. What is worth testing
here — an org user sees the global catalog but cannot edit it, a model a gateway points
at cannot be deleted, a name collides only inside its own namespace — is a rule about
state rather than about SQL, and those tests should run on every commit without a
database container.

The one thing this port is careful about is the **two kinds of read**. ``model`` answers
"may this caller *see* it", which includes the global catalog; ``owned_model`` answers
"may this caller *change* it", which does not. Every write path in
:mod:`app.services.catalog` starts from the second, so an org user editing a global model
gets the same 404 as for a model that does not exist. Two methods rather than one method
with a flag, because a flag defaulted the wrong way is a privilege bug and a missing
method is a compile error.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import Gateway, UpstreamModel
from app.db.repositories import GatewayRepository, UpstreamModelRepository, model_is_visible
from app.services.memory_db import MemoryDatabase


class CatalogTransaction(Protocol):
    """One unit of work, already scoped. Returned objects are live in both
    implementations: mutate one and commit."""

    @property
    def scope(self) -> TenantScope: ...

    async def model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        """Readable by this scope: its own models and the global catalog."""

    async def owned_model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        """Writable by this scope. A global model is owned by the platform, so this
        returns ``None`` for one unless the scope *is* the platform."""

    async def models(
        self,
        *,
        after: uuid.UUID | None,
        limit: int,
        scope_filter: str | None = None,
        enabled: bool | None = None,
    ) -> Sequence[UpstreamModel]: ...

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool: ...

    async def global_name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool: ...

    async def add_model(self, model: UpstreamModel) -> UpstreamModel: ...

    async def add_global_model(self, model: UpstreamModel) -> UpstreamModel: ...

    async def delete_model(self, model: UpstreamModel) -> None: ...

    async def gateways_referencing(self, model_id: uuid.UUID) -> Sequence[Gateway]: ...

    async def commit(self) -> None: ...


class CatalogStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[CatalogTransaction]:
        """Open a scoped transaction: ``async with store.begin(scope) as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresCatalogTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._models = UpstreamModelRepository(session, scope)
        self._gateways = GatewayRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        return await self._models.get_visible(model_id)

    async def owned_model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        return await self._models.get(model_id)

    async def models(
        self,
        *,
        after: uuid.UUID | None,
        limit: int,
        scope_filter: str | None = None,
        enabled: bool | None = None,
    ) -> Sequence[UpstreamModel]:
        return await self._models.fetch(
            self._models.page(after=after, limit=limit, scope_filter=scope_filter, enabled=enabled)
        )

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        return await self._models.name_taken(name, excluding=excluding)

    async def global_name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        return await self._models.global_name_taken(name, excluding=excluding)

    async def add_model(self, model: UpstreamModel) -> UpstreamModel:
        return await self._models.add(model)

    async def add_global_model(self, model: UpstreamModel) -> UpstreamModel:
        return await self._models.add_global(model)

    async def delete_model(self, model: UpstreamModel) -> None:
        await self._models.delete(model)

    async def gateways_referencing(self, model_id: uuid.UUID) -> Sequence[Gateway]:
        return await self._gateways.referencing(model_id)

    async def commit(self) -> None:
        await self._session.commit()


class PostgresCatalogStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[CatalogTransaction]:
        # No commit on exit: leaving without one rolls back, which is what should happen
        # to a half-applied model edit.
        async with self._session_factory() as session:
            yield PostgresCatalogTransaction(session, scope)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryCatalogTransaction:
    """Dictionaries, filtered through :func:`model_is_visible` and
    :meth:`TenantScope.permits` — the same predicates the SQL clauses are built from."""

    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        found = self._db.upstream_models.get(model_id)
        if found is None or not model_is_visible(
            self._scope, found.organization_id, enabled=found.enabled
        ):
            return None
        return found

    async def owned_model(self, model_id: uuid.UUID) -> UpstreamModel | None:
        found = self._db.upstream_models.get(model_id)
        if found is None or not self._scope.permits(found.organization_id):
            return None
        return found

    async def models(
        self,
        *,
        after: uuid.UUID | None,
        limit: int,
        scope_filter: str | None = None,
        enabled: bool | None = None,
    ) -> Sequence[UpstreamModel]:
        rows = [
            model
            for model in self._db.upstream_models.values()
            if model_is_visible(self._scope, model.organization_id, enabled=model.enabled)
            and (scope_filter is None or model.scope == scope_filter)
            and (enabled is None or model.enabled == enabled)
        ]
        # Newest first, matching ORDER BY id DESC — UUIDv7 ids sort by creation time.
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return ordered[: limit + 1]

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        wanted = name.strip()
        return any(
            model.name == wanted
            and model.id != excluding
            and self._scope.permits(model.organization_id)
            for model in self._db.upstream_models.values()
        )

    async def global_name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        wanted = name.strip()
        return any(
            model.name == wanted and model.id != excluding and model.organization_id is None
            for model in self._db.upstream_models.values()
        )

    async def add_model(self, model: UpstreamModel) -> UpstreamModel:
        model.organization_id = self._scope.require_organization()
        return self._db.add_model(model)

    async def add_global_model(self, model: UpstreamModel) -> UpstreamModel:
        model.organization_id = None
        model.scope = "global"
        return self._db.add_model(model)

    async def delete_model(self, model: UpstreamModel) -> None:
        self._db.upstream_models.pop(model.id, None)

    async def gateways_referencing(self, model_id: uuid.UUID) -> Sequence[Gateway]:
        gateway_ids = {
            target.gateway_id
            for target in self._db.gateway_targets.values()
            if target.upstream_model_id == model_id
        }
        found = [
            gateway
            for gateway in self._db.gateways.values()
            if gateway.id in gateway_ids and self._scope.permits(gateway.organization_id)
        ]
        return sorted(found, key=lambda gateway: gateway.slug)

    async def commit(self) -> None:
        return None


class MemoryCatalogStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[CatalogTransaction]:
        yield MemoryCatalogTransaction(self._db, scope)
