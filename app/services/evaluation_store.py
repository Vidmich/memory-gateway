"""Persistence for evaluation sets, their items, and their runs (task 103).

Three tables behind one transaction, because every write to one is scoped by another: an
item belongs to a set, a run measures a set, and a set belongs to a gateway. The protocol
is the usual pair — PostgreSQL and the memory twin — and the reads are the ones the screen
makes: the sets of a gateway with their item counts, one set with its items, the runs of a
set newest first, and one run whole.

Item counts come back split by what the report has to say about them — verified or not,
generated or not — because the headline over a set with forty generated items is a
different sentence from the headline over forty confirmed ones, and the list screen says so
before anybody presses Run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import EvaluationItem, EvaluationRun, EvaluationSet
from app.db.scoping import ScopedRepository, scoped
from app.services.audit import (
    AuditingTransaction,
    MemoryAuditRecorder,
    PostgresAuditRecorder,
)
from app.services.evaluation import SOURCE_GENERATED
from app.services.memory_db import MemoryDatabase

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

#: Runs listed per set. The diff view picks two of them; forty is more history than a
#: tuning session produces.
RUNS_SHOWN = 40


@dataclass(frozen=True, slots=True)
class ItemCounts:
    total: int = 0
    verified: int = 0
    generated: int = 0
    negatives: int = 0


class EvaluationTransaction(AuditingTransaction, Protocol):
    @property
    def scope(self) -> TenantScope: ...

    async def sets(self, gateway_id: uuid.UUID) -> Sequence[EvaluationSet]: ...

    async def set(self, set_id: uuid.UUID) -> EvaluationSet | None: ...

    async def add_set(self, row: EvaluationSet) -> EvaluationSet: ...

    async def delete_set(self, row: EvaluationSet) -> None: ...

    async def items(self, set_id: uuid.UUID) -> Sequence[EvaluationItem]:
        """In creation order, which is the order a run takes them in."""
        ...

    async def item(self, item_id: uuid.UUID) -> EvaluationItem | None: ...

    async def add_item(self, row: EvaluationItem) -> EvaluationItem: ...

    async def delete_item(self, row: EvaluationItem) -> None: ...

    async def item_counts(self, set_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, ItemCounts]: ...

    async def runs(self, set_id: uuid.UUID, *, limit: int = RUNS_SHOWN) -> Sequence[EvaluationRun]:
        """Newest first."""
        ...

    async def run(self, run_id: uuid.UUID) -> EvaluationRun | None: ...

    async def add_run(self, row: EvaluationRun) -> EvaluationRun: ...

    async def commit(self) -> None: ...


class EvaluationStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[EvaluationTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


class SetRepository(ScopedRepository[EvaluationSet]):
    model = EvaluationSet


class ItemRepository(ScopedRepository[EvaluationItem]):
    model = EvaluationItem


class RunRepository(ScopedRepository[EvaluationRun]):
    model = EvaluationRun


class PostgresEvaluationTransaction(PostgresAuditRecorder):
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._sets = SetRepository(session, scope)
        self._items = ItemRepository(session, scope)
        self._runs = RunRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def sets(self, gateway_id: uuid.UUID) -> Sequence[EvaluationSet]:
        statement = (
            self._sets.select()
            .where(EvaluationSet.gateway_id == gateway_id)
            .order_by(EvaluationSet.created_at, EvaluationSet.id)
        )
        return await self._sets.fetch(statement)

    async def set(self, set_id: uuid.UUID) -> EvaluationSet | None:
        return await self._sets.get(set_id)

    async def add_set(self, row: EvaluationSet) -> EvaluationSet:
        return await self._sets.add(row)

    async def delete_set(self, row: EvaluationSet) -> None:
        await self._session.delete(row)
        await self._session.flush()

    async def items(self, set_id: uuid.UUID) -> Sequence[EvaluationItem]:
        statement = (
            self._items.select()
            .where(EvaluationItem.set_id == set_id)
            .order_by(EvaluationItem.created_at, EvaluationItem.id)
        )
        return await self._items.fetch(statement)

    async def item(self, item_id: uuid.UUID) -> EvaluationItem | None:
        return await self._items.get(item_id)

    async def add_item(self, row: EvaluationItem) -> EvaluationItem:
        return await self._items.add(row)

    async def delete_item(self, row: EvaluationItem) -> None:
        await self._session.delete(row)
        await self._session.flush()

    async def item_counts(self, set_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, ItemCounts]:
        if not set_ids:
            return {}
        negative = (EvaluationItem.relevant == []) & (EvaluationItem.relevant_document_ids == [])
        statement = (
            select(
                EvaluationItem.set_id,
                func.count(),
                func.count().filter(EvaluationItem.verified.is_(True)),
                func.count().filter(EvaluationItem.source == SOURCE_GENERATED),
                func.count().filter(negative),
            )
            .where(self._scope.clause(EvaluationItem), EvaluationItem.set_id.in_(list(set_ids)))
            .group_by(EvaluationItem.set_id)
            .execution_options(**scoped())
        )
        rows = (await self._session.execute(statement)).all()
        return {
            row[0]: ItemCounts(
                total=int(row[1]),
                verified=int(row[2]),
                generated=int(row[3]),
                negatives=int(row[4]),
            )
            for row in rows
        }

    async def runs(self, set_id: uuid.UUID, *, limit: int = RUNS_SHOWN) -> Sequence[EvaluationRun]:
        statement = (
            self._runs.select()
            .where(EvaluationRun.set_id == set_id)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
            .limit(limit)
        )
        return await self._runs.fetch(statement)

    async def run(self, run_id: uuid.UUID) -> EvaluationRun | None:
        return await self._runs.get(run_id)

    async def add_run(self, row: EvaluationRun) -> EvaluationRun:
        return await self._runs.add(row)

    async def commit(self) -> None:
        await self._session.commit()


class PostgresEvaluationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[EvaluationTransaction]:
        async with self._session_factory() as session:
            yield PostgresEvaluationTransaction(session, scope)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


class MemoryEvaluationTransaction(MemoryAuditRecorder):
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def _visible(self, row: EvaluationSet | EvaluationItem | EvaluationRun | None) -> bool:
        return row is not None and (
            self._scope.organization_id is None
            or row.organization_id == self._scope.organization_id
        )

    async def sets(self, gateway_id: uuid.UUID) -> Sequence[EvaluationSet]:
        rows = [
            row
            for row in self._db.evaluation_sets.values()
            if self._visible(row) and row.gateway_id == gateway_id
        ]
        rows.sort(key=lambda row: (row.created_at, row.id))
        return rows

    async def set(self, set_id: uuid.UUID) -> EvaluationSet | None:
        row = self._db.evaluation_sets.get(set_id)
        return row if self._visible(row) else None

    async def add_set(self, row: EvaluationSet) -> EvaluationSet:
        self._db.evaluation_sets[row.id] = row
        return row

    async def delete_set(self, row: EvaluationSet) -> None:
        # The cascades the foreign keys would perform.
        for item_id in [i.id for i in self._db.evaluation_items.values() if i.set_id == row.id]:
            del self._db.evaluation_items[item_id]
        for run_id in [r.id for r in self._db.evaluation_runs.values() if r.set_id == row.id]:
            del self._db.evaluation_runs[run_id]
        self._db.evaluation_sets.pop(row.id, None)

    async def items(self, set_id: uuid.UUID) -> Sequence[EvaluationItem]:
        rows = [
            row
            for row in self._db.evaluation_items.values()
            if self._visible(row) and row.set_id == set_id
        ]
        rows.sort(key=lambda row: (row.created_at, row.id))
        return rows

    async def item(self, item_id: uuid.UUID) -> EvaluationItem | None:
        row = self._db.evaluation_items.get(item_id)
        return row if self._visible(row) else None

    async def add_item(self, row: EvaluationItem) -> EvaluationItem:
        self._db.evaluation_items[row.id] = row
        return row

    async def delete_item(self, row: EvaluationItem) -> None:
        self._db.evaluation_items.pop(row.id, None)

    async def item_counts(self, set_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, ItemCounts]:
        wanted = set(set_ids)
        counts: dict[uuid.UUID, list[int]] = {}
        for row in self._db.evaluation_items.values():
            if not self._visible(row) or row.set_id not in wanted:
                continue
            entry = counts.setdefault(row.set_id, [0, 0, 0, 0])
            entry[0] += 1
            entry[1] += 1 if row.verified else 0
            entry[2] += 1 if row.source == SOURCE_GENERATED else 0
            entry[3] += 1 if not row.relevant and not row.relevant_document_ids else 0
        return {
            set_id: ItemCounts(total=e[0], verified=e[1], generated=e[2], negatives=e[3])
            for set_id, e in counts.items()
        }

    async def runs(self, set_id: uuid.UUID, *, limit: int = RUNS_SHOWN) -> Sequence[EvaluationRun]:
        rows = [
            row
            for row in self._db.evaluation_runs.values()
            if self._visible(row) and row.set_id == set_id
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return rows[:limit]

    async def run(self, run_id: uuid.UUID) -> EvaluationRun | None:
        row = self._db.evaluation_runs.get(run_id)
        return row if self._visible(row) else None

    async def add_run(self, row: EvaluationRun) -> EvaluationRun:
        self._db.evaluation_runs[row.id] = row
        return row

    async def commit(self) -> None:
        return None


class MemoryEvaluationStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[EvaluationTransaction]:
        yield MemoryEvaluationTransaction(self._db, scope)


__all__ = [
    "FAILED",
    "QUEUED",
    "RUNNING",
    "RUNS_SHOWN",
    "SUCCEEDED",
    "EvaluationStore",
    "EvaluationTransaction",
    "ItemCounts",
    "MemoryEvaluationStore",
    "PostgresEvaluationStore",
]
