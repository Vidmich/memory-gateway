"""Persistence for reprocessing runs (task 104): one row per tracked recut of a connector.

The same shape as the other stores. The reads are the screens' questions — the run still
going on a connector, the last N for its history, the ones a platform reindex spawned —
plus the two the reconciliation job asks across every organization: which runs are still
open, and which connectors exist at all, so their stored index status can be recomputed
from their fingerprints.

The counters are *not* moved here. A document finishes inside the ingestion pipeline's
own transaction, and the increment lives beside the row write in
:meth:`~app.services.connector_store.ConnectorTransaction.count_reprocessed`, so a crash
between the two cannot leave a run whose numbers disagree with its documents.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import Connector, ReprocessingRun
from app.db.scoping import ScopedRepository, scoped
from app.services.audit import (
    AuditingTransaction,
    MemoryAuditRecorder,
    PostgresAuditRecorder,
)
from app.services.memory_db import MemoryDatabase

RUNNING = "running"


class ReprocessingTransaction(AuditingTransaction, Protocol):
    @property
    def scope(self) -> TenantScope: ...

    async def add(self, run: ReprocessingRun) -> ReprocessingRun: ...

    async def find(self, run_id: uuid.UUID) -> ReprocessingRun | None: ...

    async def running(self, connector_id: uuid.UUID) -> ReprocessingRun | None:
        """The run still open on this connector — one at a time, so a second press
        returns it rather than starting another."""
        ...

    async def runs(self, connector_id: uuid.UUID, *, limit: int) -> Sequence[ReprocessingRun]:
        """The connector's history, newest first."""
        ...

    async def spawned_by(self, reindex_run_id: uuid.UUID) -> Sequence[ReprocessingRun]:
        """The per-connector runs a platform reindex created."""
        ...

    async def unfinished(self) -> Sequence[ReprocessingRun]:
        """Every open run in the scope — the reconciliation job's input."""
        ...

    async def connectors(self) -> Sequence[Connector]:
        """Every connector in the scope, for the nightly status pass."""
        ...

    async def commit(self) -> None: ...


class ReprocessingStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[ReprocessingTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


class ReprocessingRepository(ScopedRepository[ReprocessingRun]):
    model = ReprocessingRun


class PostgresReprocessingTransaction(PostgresAuditRecorder):
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._runs = ReprocessingRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def add(self, run: ReprocessingRun) -> ReprocessingRun:
        return await self._runs.add(run)

    async def find(self, run_id: uuid.UUID) -> ReprocessingRun | None:
        return await self._runs.get(run_id)

    async def running(self, connector_id: uuid.UUID) -> ReprocessingRun | None:
        statement = (
            self._runs.select()
            .where(
                ReprocessingRun.connector_id == connector_id,
                ReprocessingRun.finished_at.is_(None),
            )
            .order_by(ReprocessingRun.started_at.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def runs(self, connector_id: uuid.UUID, *, limit: int) -> Sequence[ReprocessingRun]:
        statement = (
            self._runs.select()
            .where(ReprocessingRun.connector_id == connector_id)
            .order_by(ReprocessingRun.started_at.desc(), ReprocessingRun.id.desc())
            .limit(limit)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def spawned_by(self, reindex_run_id: uuid.UUID) -> Sequence[ReprocessingRun]:
        statement = (
            self._runs.select()
            .where(ReprocessingRun.reindex_run_id == reindex_run_id)
            .order_by(ReprocessingRun.started_at, ReprocessingRun.id)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def unfinished(self) -> Sequence[ReprocessingRun]:
        statement = (
            self._runs.select()
            .where(ReprocessingRun.finished_at.is_(None))
            .order_by(ReprocessingRun.started_at)
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def connectors(self) -> Sequence[Connector]:
        statement = (
            select(Connector)
            .where(self._scope.clause(Connector))
            .order_by(Connector.id)
            .execution_options(**scoped())
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def commit(self) -> None:
        await self._session.commit()


class PostgresReprocessingStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[ReprocessingTransaction]:
        async with self._session_factory() as session:
            yield PostgresReprocessingTransaction(session, scope)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


class MemoryReprocessingTransaction(MemoryAuditRecorder):
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def _mine(self) -> list[ReprocessingRun]:
        return [
            row
            for row in self._db.reprocessing_runs.values()
            if self._scope.permits(row.organization_id)
        ]

    async def add(self, run: ReprocessingRun) -> ReprocessingRun:
        self._db.reprocessing_runs[run.id] = run
        return run

    async def find(self, run_id: uuid.UUID) -> ReprocessingRun | None:
        row = self._db.reprocessing_runs.get(run_id)
        if row is None or not self._scope.permits(row.organization_id):
            return None
        return row

    async def running(self, connector_id: uuid.UUID) -> ReprocessingRun | None:
        rows = [
            row
            for row in self._mine()
            if row.connector_id == connector_id and row.finished_at is None
        ]
        rows.sort(key=lambda row: row.started_at, reverse=True)
        return rows[0] if rows else None

    async def runs(self, connector_id: uuid.UUID, *, limit: int) -> Sequence[ReprocessingRun]:
        rows = [row for row in self._mine() if row.connector_id == connector_id]
        rows.sort(key=lambda row: (row.started_at, row.id), reverse=True)
        return rows[:limit]

    async def spawned_by(self, reindex_run_id: uuid.UUID) -> Sequence[ReprocessingRun]:
        rows = [row for row in self._mine() if row.reindex_run_id == reindex_run_id]
        rows.sort(key=lambda row: (row.started_at, row.id))
        return rows

    async def unfinished(self) -> Sequence[ReprocessingRun]:
        rows = [row for row in self._mine() if row.finished_at is None]
        rows.sort(key=lambda row: row.started_at)
        return rows

    async def connectors(self) -> Sequence[Connector]:
        return sorted(
            (
                row
                for row in self._db.connectors.values()
                if self._scope.permits(row.organization_id)
            ),
            key=lambda row: row.id,
        )

    async def commit(self) -> None:
        return None


class MemoryReprocessingStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[ReprocessingTransaction]:
        yield MemoryReprocessingTransaction(self._db, scope)


__all__ = [
    "RUNNING",
    "MemoryReprocessingStore",
    "MemoryReprocessingTransaction",
    "PostgresReprocessingStore",
    "PostgresReprocessingTransaction",
    "ReprocessingStore",
    "ReprocessingTransaction",
]
