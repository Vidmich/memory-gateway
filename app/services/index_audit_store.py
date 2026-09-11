"""Persistence for index audits (task 103): one row per audit, the latest per connector.

The same shape as the other stores — a transaction protocol, PostgreSQL behind it, and the
memory twin over :class:`~app.services.memory_db.MemoryDatabase` — with three reads that
are the three questions the screens ask: *the* latest report for a connector and kind, the
one still running so a second click does not start a second scroll, and the organization's
red findings for the dashboard.

Mixes in the audit recorder because starting an audit is an audited action: the embedding
audit can spend at the provider, and a spend has to be attributable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import IndexAudit
from app.db.scoping import ScopedRepository, scoped
from app.services.audit import (
    AuditingTransaction,
    MemoryAuditRecorder,
    PostgresAuditRecorder,
)
from app.services.memory_db import MemoryDatabase

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class IndexAuditTransaction(AuditingTransaction, Protocol):
    @property
    def scope(self) -> TenantScope: ...

    async def add(self, audit: IndexAudit) -> IndexAudit: ...

    async def find(self, audit_id: uuid.UUID) -> IndexAudit | None: ...

    async def latest(self, connector_id: uuid.UUID, kind: str) -> IndexAudit | None:
        """The newest audit of this kind for this connector, whatever its status."""
        ...

    async def running(self, connector_id: uuid.UUID, kind: str) -> IndexAudit | None: ...

    async def latest_all(self) -> Sequence[IndexAudit]:
        """The newest audit per (connector, kind) across the organization — what the
        dashboard reads for red findings, and the connectors list for badges."""
        ...

    async def commit(self) -> None: ...


class IndexAuditStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[IndexAuditTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


class IndexAuditRepository(ScopedRepository[IndexAudit]):
    model = IndexAudit


class PostgresIndexAuditTransaction(PostgresAuditRecorder):
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._audits = IndexAuditRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def add(self, audit: IndexAudit) -> IndexAudit:
        return await self._audits.add(audit)

    async def find(self, audit_id: uuid.UUID) -> IndexAudit | None:
        return await self._audits.get(audit_id)

    async def latest(self, connector_id: uuid.UUID, kind: str) -> IndexAudit | None:
        statement = (
            self._audits.select()
            .where(IndexAudit.connector_id == connector_id, IndexAudit.kind == kind)
            .order_by(IndexAudit.created_at.desc(), IndexAudit.id.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def running(self, connector_id: uuid.UUID, kind: str) -> IndexAudit | None:
        statement = (
            self._audits.select()
            .where(
                IndexAudit.connector_id == connector_id,
                IndexAudit.kind == kind,
                IndexAudit.status == RUNNING,
            )
            .order_by(IndexAudit.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def latest_all(self) -> Sequence[IndexAudit]:
        statement = (
            select(IndexAudit)
            .where(self._scope.clause(IndexAudit))
            .distinct(IndexAudit.connector_id, IndexAudit.kind)
            .order_by(
                IndexAudit.connector_id,
                IndexAudit.kind,
                IndexAudit.created_at.desc(),
                IndexAudit.id.desc(),
            )
            .execution_options(**scoped())
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def commit(self) -> None:
        await self._session.commit()


class PostgresIndexAuditStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[IndexAuditTransaction]:
        async with self._session_factory() as session:
            yield PostgresIndexAuditTransaction(session, scope)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


class MemoryIndexAuditTransaction(MemoryAuditRecorder):
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def _mine(self) -> list[IndexAudit]:
        return [
            row
            for row in self._db.index_audits.values()
            if self._scope.organization_id is None
            or row.organization_id == self._scope.organization_id
        ]

    async def add(self, audit: IndexAudit) -> IndexAudit:
        self._db.index_audits[audit.id] = audit
        return audit

    async def find(self, audit_id: uuid.UUID) -> IndexAudit | None:
        row = self._db.index_audits.get(audit_id)
        if row is None or row not in self._mine():
            return None
        return row

    async def latest(self, connector_id: uuid.UUID, kind: str) -> IndexAudit | None:
        rows = [
            row for row in self._mine() if row.connector_id == connector_id and row.kind == kind
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return rows[0] if rows else None

    async def running(self, connector_id: uuid.UUID, kind: str) -> IndexAudit | None:
        rows = [
            row
            for row in self._mine()
            if row.connector_id == connector_id and row.kind == kind and row.status == RUNNING
        ]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[0] if rows else None

    async def latest_all(self) -> Sequence[IndexAudit]:
        newest: dict[tuple[uuid.UUID, str], IndexAudit] = {}
        for row in sorted(self._mine(), key=lambda row: (row.created_at, row.id)):
            newest[(row.connector_id, row.kind)] = row
        return [newest[key] for key in sorted(newest, key=lambda key: (str(key[0]), key[1]))]

    async def commit(self) -> None:
        return None


class MemoryIndexAuditStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[IndexAuditTransaction]:
        yield MemoryIndexAuditTransaction(self._db, scope)


__all__ = [
    "FAILED",
    "RUNNING",
    "SUCCEEDED",
    "IndexAuditStore",
    "IndexAuditTransaction",
    "MemoryIndexAuditStore",
    "PostgresIndexAuditStore",
]
