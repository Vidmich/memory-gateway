"""Persistence for the summarization ledger (task 102): run rows in, health out.

The same shape as :mod:`app.services.distillation_store`, for the same reasons, with the
one addition that table exists for: tokens. Three reads and one write.

**A run row is written for every attempt that reached a decision** — succeeded, failed, or
refused by the cap — because the panel's rates need a denominator and the cap needs a
count. The one thing not recorded is a document whose format is not summarized at all: a
row per "not applicable" would be most of the table on a repository connector.

**The daily cap is a count over this table, per connector.** Not a Redis counter: the number
the connector screen shows has to be the number the guard used. Refusals do not count
toward the cap that refused them, or it would latch on for the rest of the day.

**Waiting documents are read from ``documents``, not from here.** A document that is
``pending`` with reason ``summarization_cap`` is the cap's visible consequence, and it is
the document row that says so; the dashboard's "N documents waiting on the summarization
cap" is a count of those rows grouped by connector.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Connector, Document, SummarizationRun
from app.db.models.summarization import MAX_REASON_LENGTH
from app.db.scoping import ScopedRepository, scoped
from app.services.memory_db import MemoryDatabase

SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"

#: ``reason`` on a skipped row when the cap refused it, and on a document row that is
#: waiting for it. One constant for both so the panel and the guard cannot disagree.
DAILY_CAP = "daily_cap_reached"
WAITING_ON_CAP = "summarization_cap"
#: ``reason`` on a skipped row when nothing is configured to summarize with.
NO_MODEL = "no_summarization_model"

#: How many connectors the panel names as the biggest spenders.
TOP_CONNECTORS = 5

#: What a row's model call was for. The panel and the cap read ``summary`` rows only;
#: ``evaluation`` rows (task 103's generated questions) are the same bill through the same
#: chain and are counted on the evaluation set that spent them.
PURPOSE_SUMMARY = "summary"
PURPOSE_EVALUATION = "evaluation"


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What one attempt did, on its way to a row."""

    organization_id: uuid.UUID
    connector_id: uuid.UUID
    document_id: uuid.UUID
    outcome: str
    reason: str | None = None
    model_id: uuid.UUID | None = None
    model_name: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    estimated: bool = False
    duration_ms: int = 0
    purpose: str = PURPOSE_SUMMARY


@dataclass(frozen=True, slots=True)
class HealthDay:
    day: datetime
    documents: int = 0
    failures: int = 0
    capped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True, slots=True)
class ModelSpend:
    model_name: str
    runs: int
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class ConnectorSpend:
    connector_id: uuid.UUID
    name: str | None
    documents: int
    tokens_in: int
    tokens_out: int


@dataclass(frozen=True, slots=True)
class WaitingConnector:
    """Documents parked on the cap, per connector — the dashboard's degraded state."""

    connector_id: uuid.UUID
    name: str | None
    documents: int


@dataclass(frozen=True, slots=True)
class SummarizationHealth:
    """The Monitoring panel's block: per day, per model, per connector, and the totals.

    The rates are properties, not columns, for the reason
    :class:`~app.services.distillation_store.MemoryHealth` gives: a stored ratio is a
    number that can disagree with its own inputs.
    """

    days: tuple[HealthDay, ...] = ()
    runs: int = 0
    documents: int = 0
    failures: int = 0
    capped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    #: Rows whose token counts are ours rather than the provider's.
    estimated_runs: int = 0
    by_model: tuple[ModelSpend, ...] = ()
    top_connectors: tuple[ConnectorSpend, ...] = ()
    waiting: tuple[WaitingConnector, ...] = ()

    @property
    def failure_rate(self) -> float:
        return self.failures / self.runs if self.runs else 0.0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def waiting_documents(self) -> int:
        return sum(entry.documents for entry in self.waiting)


class SummarizationTransaction(Protocol):
    @property
    def scope(self) -> TenantScope: ...

    async def record(self, run: RunRecord) -> SummarizationRun: ...

    async def documents_since(self, connector_id: uuid.UUID, since: datetime) -> int:
        """Attempts that reached the model for this connector since ``since`` — the cap's
        numerator. Skipped rows do not count."""
        ...

    async def health(
        self, *, start: datetime, end: datetime, connector_id: uuid.UUID | None = None
    ) -> SummarizationHealth: ...

    async def commit(self) -> None: ...


class SummarizationStore(Protocol):
    def begin(
        self, scope: TenantScope
    ) -> AbstractAsyncContextManager[SummarizationTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


class SummarizationRunRepository(ScopedRepository[SummarizationRun]):
    model = SummarizationRun


class PostgresSummarizationTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def record(self, run: RunRecord) -> SummarizationRun:
        row = _row_of(run)
        self._session.add(row)
        await self._session.flush()
        return row

    async def documents_since(self, connector_id: uuid.UUID, since: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(SummarizationRun)
            .where(
                self._scope.clause(SummarizationRun),
                SummarizationRun.connector_id == connector_id,
                SummarizationRun.created_at >= since,
                SummarizationRun.outcome.in_((SUCCEEDED, FAILED)),
                SummarizationRun.purpose == PURPOSE_SUMMARY,
            )
            .execution_options(**scoped())
        )
        return int((await self._session.execute(statement)).scalar() or 0)

    async def health(
        self, *, start: datetime, end: datetime, connector_id: uuid.UUID | None = None
    ) -> SummarizationHealth:
        window = [
            self._scope.clause(SummarizationRun),
            SummarizationRun.created_at >= start,
            SummarizationRun.created_at < end,
            SummarizationRun.purpose == PURPOSE_SUMMARY,
        ]
        if connector_id is not None:
            window.append(SummarizationRun.connector_id == connector_id)

        day = func.date_trunc("day", SummarizationRun.created_at).label("day")
        capped = (SummarizationRun.outcome == SKIPPED) & (SummarizationRun.reason == DAILY_CAP)
        per_day = (
            select(
                day,
                func.count().filter(SummarizationRun.outcome == SUCCEEDED),
                func.count().filter(SummarizationRun.outcome == FAILED),
                func.count().filter(capped),
                func.coalesce(func.sum(SummarizationRun.tokens_in), 0),
                func.coalesce(func.sum(SummarizationRun.tokens_out), 0),
                func.count(),
                func.count().filter(SummarizationRun.estimated.is_(True)),
            )
            .where(*window)
            .group_by(day)
            .order_by(day)
            .execution_options(**scoped())
        )
        days = (await self._session.execute(per_day)).all()

        per_model = (
            select(
                SummarizationRun.model_name,
                func.count(),
                func.coalesce(func.sum(SummarizationRun.tokens_in), 0),
                func.coalesce(func.sum(SummarizationRun.tokens_out), 0),
            )
            .where(*window, SummarizationRun.outcome.in_((SUCCEEDED, FAILED)))
            .group_by(SummarizationRun.model_name)
            .order_by(func.sum(SummarizationRun.tokens_in + SummarizationRun.tokens_out).desc())
            .execution_options(**scoped())
        )
        models = (await self._session.execute(per_model)).all()

        spend = SummarizationRun.tokens_in + SummarizationRun.tokens_out
        per_connector = (
            select(
                SummarizationRun.connector_id,
                Connector.name,
                func.count().filter(SummarizationRun.outcome == SUCCEEDED),
                func.coalesce(func.sum(SummarizationRun.tokens_in), 0),
                func.coalesce(func.sum(SummarizationRun.tokens_out), 0),
            )
            .select_from(SummarizationRun)
            .outerjoin(Connector, Connector.id == SummarizationRun.connector_id)
            .where(*window)
            .group_by(SummarizationRun.connector_id, Connector.name)
            .order_by(func.sum(spend).desc())
            .limit(TOP_CONNECTORS)
            .execution_options(**scoped())
        )
        connectors = (await self._session.execute(per_connector)).all()

        parked = [
            self._scope.clause(Document),
            Document.status == "pending",
            Document.reason == WAITING_ON_CAP,
        ]
        if connector_id is not None:
            parked.append(Document.connector_id == connector_id)
        waiting_statement = (
            select(Document.connector_id, Connector.name, func.count())
            .select_from(Document)
            .outerjoin(Connector, Connector.id == Document.connector_id)
            .where(*parked)
            .group_by(Document.connector_id, Connector.name)
            .order_by(func.count().desc())
            .execution_options(**scoped())
        )
        waiting = (await self._session.execute(waiting_statement)).all()
        return _health_of(days, models, connectors, waiting)

    async def commit(self) -> None:
        await self._session.commit()


class PostgresSummarizationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[SummarizationTransaction]:
        async with self._session_factory() as session:
            yield PostgresSummarizationTransaction(session, scope)


def _row_of(run: RunRecord) -> SummarizationRun:
    return SummarizationRun(
        id=uuid7(),
        organization_id=run.organization_id,
        connector_id=run.connector_id,
        document_id=run.document_id,
        outcome=run.outcome,
        reason=(run.reason or None) and run.reason[:MAX_REASON_LENGTH],
        model_id=run.model_id,
        model_name=run.model_name,
        tokens_in=max(0, run.tokens_in),
        tokens_out=max(0, run.tokens_out),
        estimated=run.estimated,
        duration_ms=run.duration_ms,
        purpose=run.purpose,
        created_at=datetime.now(UTC),
    )


def _health_of(
    days: Sequence[Any],
    models: Sequence[Any],
    connectors: Sequence[Any],
    waiting: Sequence[Any],
) -> SummarizationHealth:
    return SummarizationHealth(
        days=tuple(
            HealthDay(
                day=row[0],
                documents=int(row[1]),
                failures=int(row[2]),
                capped=int(row[3]),
                tokens_in=int(row[4]),
                tokens_out=int(row[5]),
            )
            for row in days
        ),
        runs=sum(int(row[6]) for row in days),
        documents=sum(int(row[1]) for row in days),
        failures=sum(int(row[2]) for row in days),
        capped=sum(int(row[3]) for row in days),
        tokens_in=sum(int(row[4]) for row in days),
        tokens_out=sum(int(row[5]) for row in days),
        estimated_runs=sum(int(row[7]) for row in days),
        by_model=tuple(
            ModelSpend(
                model_name=str(row[0] or "unknown"),
                runs=int(row[1]),
                tokens_in=int(row[2]),
                tokens_out=int(row[3]),
            )
            for row in models
        ),
        top_connectors=tuple(
            ConnectorSpend(
                connector_id=row[0],
                name=row[1],
                documents=int(row[2]),
                tokens_in=int(row[3]),
                tokens_out=int(row[4]),
            )
            for row in connectors
        ),
        waiting=tuple(
            WaitingConnector(connector_id=row[0], name=row[1], documents=int(row[2]))
            for row in waiting
        ),
    )


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemorySummarizationTransaction:
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def record(self, run: RunRecord) -> SummarizationRun:
        row = _row_of(run)
        self._db.summarization_runs[row.id] = row
        return row

    async def documents_since(self, connector_id: uuid.UUID, since: datetime) -> int:
        return sum(
            1
            for row in self._db.summarization_runs.values()
            if self._scope.permits(row.organization_id)
            and row.connector_id == connector_id
            and row.created_at >= since
            and row.outcome in (SUCCEEDED, FAILED)
            and row.purpose == PURPOSE_SUMMARY
        )

    async def health(
        self, *, start: datetime, end: datetime, connector_id: uuid.UUID | None = None
    ) -> SummarizationHealth:
        rows = [
            row
            for row in self._db.summarization_runs.values()
            if self._scope.permits(row.organization_id)
            and start <= row.created_at < end
            and row.purpose == PURPOSE_SUMMARY
            and (connector_id is None or row.connector_id == connector_id)
        ]
        buckets: dict[datetime, list[int]] = {}
        for row in rows:
            day = row.created_at.replace(hour=0, minute=0, second=0, microsecond=0)
            bucket = buckets.setdefault(day, [0] * 8)
            bucket[0] += 1 if row.outcome == SUCCEEDED else 0
            bucket[1] += 1 if row.outcome == FAILED else 0
            bucket[2] += 1 if row.outcome == SKIPPED and row.reason == DAILY_CAP else 0
            bucket[3] += row.tokens_in
            bucket[4] += row.tokens_out
            bucket[5] += 1
            bucket[6] += 1 if row.estimated else 0
        days = [(day, *bucket) for day, bucket in sorted(buckets.items())]

        models: dict[str | None, list[int]] = {}
        for row in rows:
            if row.outcome not in (SUCCEEDED, FAILED):
                continue
            entry = models.setdefault(row.model_name, [0, 0, 0])
            entry[0] += 1
            entry[1] += row.tokens_in
            entry[2] += row.tokens_out
        by_model: list[tuple[Any, ...]] = [
            (name, *counts)
            for name, counts in sorted(
                models.items(), key=lambda entry: -(entry[1][1] + entry[1][2])
            )
        ]

        spend: dict[uuid.UUID, list[int]] = {}
        for row in rows:
            entry = spend.setdefault(row.connector_id, [0, 0, 0])
            entry[0] += 1 if row.outcome == SUCCEEDED else 0
            entry[1] += row.tokens_in
            entry[2] += row.tokens_out
        ranked = sorted(spend.items(), key=lambda entry: -(entry[1][1] + entry[1][2]))
        connectors: list[tuple[Any, ...]] = [
            (cid, self._name(cid), *counts) for cid, counts in ranked[:TOP_CONNECTORS]
        ]

        parked: dict[uuid.UUID, int] = {}
        for document in self._db.documents.values():
            if not self._scope.permits(document.organization_id):
                continue
            if document.status != "pending" or document.reason != WAITING_ON_CAP:
                continue
            if connector_id is not None and document.connector_id != connector_id:
                continue
            parked[document.connector_id] = parked.get(document.connector_id, 0) + 1
        waiting = sorted(
            ((cid, self._name(cid), count) for cid, count in parked.items()),
            key=lambda entry: -entry[2],
        )
        return _health_of(days, by_model, connectors, waiting)

    def _name(self, connector_id: uuid.UUID) -> str | None:
        connector = self._db.connectors.get(connector_id)
        return connector.name if connector is not None else None

    async def commit(self) -> None:
        return None


@dataclass
class MemorySummarizationStore:
    database: MemoryDatabase = field(default_factory=MemoryDatabase)

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[SummarizationTransaction]:
        yield MemorySummarizationTransaction(self.database, scope)


__all__ = [
    "DAILY_CAP",
    "FAILED",
    "NO_MODEL",
    "PURPOSE_EVALUATION",
    "PURPOSE_SUMMARY",
    "SKIPPED",
    "SUCCEEDED",
    "TOP_CONNECTORS",
    "WAITING_ON_CAP",
    "ConnectorSpend",
    "HealthDay",
    "MemorySummarizationStore",
    "MemorySummarizationTransaction",
    "ModelSpend",
    "PostgresSummarizationStore",
    "PostgresSummarizationTransaction",
    "RunRecord",
    "SummarizationHealth",
    "SummarizationStore",
    "SummarizationTransaction",
    "WaitingConnector",
]
