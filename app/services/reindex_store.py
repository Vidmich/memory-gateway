"""Reindex runs and their per-organization targets.

Two rows for one job, and the split is what makes resumption per tenant rather than per
platform. A reindex of forty organizations that dies on the thirty-ninth must not re-embed
the first thirty-eight, and the only way to promise that is for each of them to carry its
own cursor and its own terminal state.

``running`` is also the concurrency guard. SPEC has no lock table and this task needs one
answer to "is a reindex already going", so the answer is a query: a run whose status is
``running`` and whose scope overlaps the requested one blocks the request. Overlap rather
than equality, because a platform-wide reindex and an organization-scoped one for a tenant
inside it are the same work happening twice, and the second one would fight the first over
the same alias.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import ReindexRun, ReindexTarget
from app.db.scoping import unscoped
from app.services.memory_db import MemoryDatabase

#: Why these statements span organizations: a reindex is a platform operation whose whole
#: purpose is to touch every tenant's collection at once.
_REASON = "a reindex rebuilds every organization's collection"

PLATFORM = "platform"
ORGANIZATION = "organization"

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

PENDING = "pending"
EMBEDDING = "embedding"
VERIFYING = "verifying"
SWAPPED = "swapped"


@dataclass(frozen=True, slots=True)
class TargetView:
    id: uuid.UUID
    run_id: uuid.UUID
    organization_id: uuid.UUID
    collection: str
    status: str = PENDING
    total_points: int = 0
    done_points: int = 0
    cursor: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunView:
    id: uuid.UUID
    scope: str
    status: str
    to_model: str
    to_dimension: int
    started_at: datetime
    organization_id: uuid.UUID | None = None
    from_model: str | None = None
    from_dimension: int | None = None
    estimated_points: int = 0
    estimated_tokens: int = 0
    finished_at: datetime | None = None
    started_by: uuid.UUID | None = None
    error: str | None = None
    targets: tuple[TargetView, ...] = ()

    def covers(self, organization_id: uuid.UUID | None) -> bool:
        """Whether this run's scope includes the given organization.

        A platform run covers everybody, including a request that names one tenant; a
        tenant-scoped run covers only itself, and — read the other way round — blocks a
        platform run, because the platform run would rebuild the same collection.
        """
        return (
            self.organization_id is None
            or organization_id is None
            or (self.organization_id == organization_id)
        )


class ReindexTransaction(Protocol):
    async def running(self) -> list[RunView]: ...

    async def create_run(
        self,
        *,
        scope: str,
        organization_id: uuid.UUID | None,
        from_model: str | None,
        from_dimension: int | None,
        to_model: str,
        to_dimension: int,
        estimated_points: int,
        estimated_tokens: int,
        started_by: uuid.UUID | None,
    ) -> RunView: ...

    async def add_target(
        self, run_id: uuid.UUID, *, organization_id: uuid.UUID, collection: str, total: int
    ) -> TargetView: ...

    async def save_target(
        self,
        target_id: uuid.UUID,
        *,
        status: str | None = None,
        done_points: int | None = None,
        cursor: str | None = None,
        error: str | None = None,
        clear_cursor: bool = False,
    ) -> None: ...

    async def finish_run(
        self, run_id: uuid.UUID, *, status: str, error: str | None = None
    ) -> None: ...

    async def run(self, run_id: uuid.UUID) -> RunView | None: ...

    async def recent(self, *, limit: int = 10) -> list[RunView]: ...

    async def commit(self) -> None: ...


class ReindexStore(Protocol):
    def begin(self) -> AbstractAsyncContextManager[ReindexTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


class PostgresReindexTransaction:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def running(self) -> list[RunView]:
        rows = await self._session.execute(
            select(ReindexRun)
            .where(ReindexRun.status == RUNNING)
            .order_by(ReindexRun.started_at.desc())
            .execution_options(**unscoped(_REASON))
        )
        return [await self._with_targets(row) for row in rows.scalars().all()]

    async def create_run(
        self,
        *,
        scope: str,
        organization_id: uuid.UUID | None,
        from_model: str | None,
        from_dimension: int | None,
        to_model: str,
        to_dimension: int,
        estimated_points: int,
        estimated_tokens: int,
        started_by: uuid.UUID | None,
    ) -> RunView:
        run = ReindexRun(
            id=uuid7(),
            scope=scope,
            scope_organization_id=organization_id,
            status=RUNNING,
            from_model=from_model,
            from_dimension=from_dimension,
            to_model=to_model,
            to_dimension=to_dimension,
            estimated_points=estimated_points,
            estimated_tokens=estimated_tokens,
            started_at=datetime.now(UTC),
            started_by=started_by,
        )
        self._session.add(run)
        await self._session.flush()
        return _run_view(run, ())

    async def add_target(
        self, run_id: uuid.UUID, *, organization_id: uuid.UUID, collection: str, total: int
    ) -> TargetView:
        target = ReindexTarget(
            id=uuid7(),
            run_id=run_id,
            organization_id=organization_id,
            collection=collection,
            status=PENDING,
            total_points=total,
            done_points=0,
        )
        self._session.add(target)
        await self._session.flush()
        return _target_view(target)

    async def save_target(
        self,
        target_id: uuid.UUID,
        *,
        status: str | None = None,
        done_points: int | None = None,
        cursor: str | None = None,
        error: str | None = None,
        clear_cursor: bool = False,
    ) -> None:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
            if status == EMBEDDING:
                values["started_at"] = datetime.now(UTC)
            elif status in (SWAPPED, FAILED):
                values["finished_at"] = datetime.now(UTC)
        if done_points is not None:
            values["done_points"] = done_points
        if clear_cursor:
            values["cursor"] = None
        elif cursor is not None:
            values["cursor"] = cursor
        if error is not None:
            values["error"] = error
        if not values:
            return
        await self._session.execute(
            update(ReindexTarget)
            .where(ReindexTarget.id == target_id)
            .values(**values)
            .execution_options(**unscoped(_REASON))
        )

    async def finish_run(self, run_id: uuid.UUID, *, status: str, error: str | None = None) -> None:
        await self._session.execute(
            update(ReindexRun)
            .where(ReindexRun.id == run_id)
            .values(status=status, error=error, finished_at=datetime.now(UTC))
            .execution_options(**unscoped(_REASON))
        )

    async def run(self, run_id: uuid.UUID) -> RunView | None:
        rows = await self._session.execute(
            select(ReindexRun).where(ReindexRun.id == run_id).execution_options(**unscoped(_REASON))
        )
        row = rows.scalars().first()
        return await self._with_targets(row) if row is not None else None

    async def recent(self, *, limit: int = 10) -> list[RunView]:
        rows = await self._session.execute(
            select(ReindexRun)
            .order_by(ReindexRun.started_at.desc())
            .limit(limit)
            .execution_options(**unscoped(_REASON))
        )
        return [await self._with_targets(row) for row in rows.scalars().all()]

    async def _with_targets(self, run: ReindexRun) -> RunView:
        rows = await self._session.execute(
            select(ReindexTarget)
            .where(ReindexTarget.run_id == run.id)
            .order_by(ReindexTarget.id)
            .execution_options(**unscoped(_REASON))
        )
        return _run_view(run, tuple(_target_view(row) for row in rows.scalars().all()))

    async def commit(self) -> None:
        await self._session.commit()


class PostgresReindexStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[ReindexTransaction]:
        async with self._session_factory() as session:
            yield PostgresReindexTransaction(session)


def _run_view(run: ReindexRun, targets: Sequence[TargetView]) -> RunView:
    return RunView(
        id=run.id,
        scope=run.scope,
        status=run.status,
        to_model=run.to_model,
        to_dimension=run.to_dimension,
        started_at=run.started_at,
        organization_id=run.scope_organization_id,
        from_model=run.from_model,
        from_dimension=run.from_dimension,
        estimated_points=run.estimated_points,
        estimated_tokens=run.estimated_tokens,
        finished_at=run.finished_at,
        started_by=run.started_by,
        error=run.error,
        targets=tuple(targets),
    )


def _target_view(target: ReindexTarget) -> TargetView:
    return TargetView(
        id=target.id,
        run_id=target.run_id,
        organization_id=target.organization_id,
        collection=target.collection,
        status=target.status,
        total_points=target.total_points,
        done_points=target.done_points,
        cursor=target.cursor,
        started_at=target.started_at,
        finished_at=target.finished_at,
        error=target.error,
    )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryReindexTransaction:
    db: MemoryDatabase

    async def running(self) -> list[RunView]:
        return [
            self._view(run)
            for run in sorted(
                self.db.reindex_runs.values(), key=lambda row: row.started_at, reverse=True
            )
            if run.status == RUNNING
        ]

    async def create_run(
        self,
        *,
        scope: str,
        organization_id: uuid.UUID | None,
        from_model: str | None,
        from_dimension: int | None,
        to_model: str,
        to_dimension: int,
        estimated_points: int,
        estimated_tokens: int,
        started_by: uuid.UUID | None,
    ) -> RunView:
        run = ReindexRun(
            id=uuid7(),
            scope=scope,
            scope_organization_id=organization_id,
            status=RUNNING,
            from_model=from_model,
            from_dimension=from_dimension,
            to_model=to_model,
            to_dimension=to_dimension,
            estimated_points=estimated_points,
            estimated_tokens=estimated_tokens,
            started_at=datetime.now(UTC),
            started_by=started_by,
        )
        self.db.reindex_runs[run.id] = run
        return _run_view(run, ())

    async def add_target(
        self, run_id: uuid.UUID, *, organization_id: uuid.UUID, collection: str, total: int
    ) -> TargetView:
        target = ReindexTarget(
            id=uuid7(),
            run_id=run_id,
            organization_id=organization_id,
            collection=collection,
            status=PENDING,
            total_points=total,
            done_points=0,
        )
        self.db.reindex_targets[target.id] = target
        return _target_view(target)

    async def save_target(
        self,
        target_id: uuid.UUID,
        *,
        status: str | None = None,
        done_points: int | None = None,
        cursor: str | None = None,
        error: str | None = None,
        clear_cursor: bool = False,
    ) -> None:
        target = self.db.reindex_targets.get(target_id)
        if target is None:
            return
        if status is not None:
            target.status = status
            if status == EMBEDDING:
                target.started_at = datetime.now(UTC)
            elif status in (SWAPPED, FAILED):
                target.finished_at = datetime.now(UTC)
        if done_points is not None:
            target.done_points = done_points
        if clear_cursor:
            target.cursor = None
        elif cursor is not None:
            target.cursor = cursor
        if error is not None:
            target.error = error

    async def finish_run(self, run_id: uuid.UUID, *, status: str, error: str | None = None) -> None:
        run = self.db.reindex_runs.get(run_id)
        if run is None:
            return
        run.status = status
        run.error = error
        run.finished_at = datetime.now(UTC)

    async def run(self, run_id: uuid.UUID) -> RunView | None:
        row = self.db.reindex_runs.get(run_id)
        return self._view(row) if row is not None else None

    async def recent(self, *, limit: int = 10) -> list[RunView]:
        found = sorted(self.db.reindex_runs.values(), key=lambda row: row.started_at, reverse=True)
        return [self._view(row) for row in found[:limit]]

    def _view(self, run: ReindexRun) -> RunView:
        targets = tuple(
            _target_view(row)
            for row in sorted(self.db.reindex_targets.values(), key=lambda row: row.id)
            if row.run_id == run.id
        )
        return _run_view(run, targets)

    async def commit(self) -> None:
        return None


@dataclass
class MemoryReindexStore:
    db: MemoryDatabase
    _pending: list[Any] = field(default_factory=list)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[ReindexTransaction]:
        yield MemoryReindexTransaction(self.db)


__all__ = [
    "EMBEDDING",
    "FAILED",
    "ORGANIZATION",
    "PENDING",
    "PLATFORM",
    "RUNNING",
    "SUCCEEDED",
    "SWAPPED",
    "VERIFYING",
    "MemoryReindexStore",
    "MemoryReindexTransaction",
    "PostgresReindexStore",
    "PostgresReindexTransaction",
    "ReindexStore",
    "ReindexTransaction",
    "RunView",
    "TargetView",
]
