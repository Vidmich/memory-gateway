"""Persistence for the distillation worker: transcripts in, run records out.

A store of its own rather than more methods on :mod:`app.services.metrics_store`, because
the two read the same two tables with opposite intent. Monitoring reads *a window of
traffic* for one organization and never cares whether a row has been distilled; this reads
*one thread's undistilled bodies* and cares about nothing else. Folding them together would
mean every monitoring filter grew a ``distilled_at`` clause that no chart uses.

Three things here are load-bearing.

**``distilled_at`` is the idempotency key.** It is what makes a backfill safe to run twice
and a duplicate job a no-op, and it is set *after* the facts are written — so a crash
between the two costs one repeated pass, which the dedupe threshold then absorbs, rather
than one silently skipped conversation. The other order would lose material permanently,
which is the failure this feature cannot recover from: the transcript will be dropped by
retention long before anybody notices the memory is thin.

**A run row is written even when the pass failed.** SPEC §10.1's health signals are rates,
and a rate whose denominator only counts successes is not a rate. The one thing not
recorded is the ordinary "nothing new to read" — a row per no-op job would be most of the
table and would drown every rate in noise.

**The daily cap is a count over this table.** Not a Redis counter: the number the Settings
screen shows has to be the number the guard actually used, and two sources that agree most
of the time are worse than one that is slower.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import DistillationRun, MemoryFact, RequestLog, Transcript
from app.db.models.distillation import MAX_REASON_LENGTH
from app.db.scoping import ScopedRepository, scoped
from app.services.end_user_store import is_live
from app.services.memory_db import MemoryDatabase

SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"

#: How many transcripts one pass reads at most. A conversation that produced more turns
#: than this since the last pass is being distilled from its tail, which is the same choice
#: :data:`app.services.distillation.MAX_EXCHANGE_CHARS` makes for the same reason — and the
#: ones left behind still get their ``distilled_at`` set, because they *were* covered by
#: the exchange the tail came from.
MAX_TRANSCRIPTS_PER_PASS = 40


@dataclass(frozen=True, slots=True)
class PendingTranscript:
    """One logged request/response pair that has not been distilled yet."""

    log_id: uuid.UUID
    created_at: datetime
    request_body: Any
    response_body: str | None


@dataclass(frozen=True, slots=True)
class PendingSession:
    """A conversation with undistilled transcripts. The backfill's unit of work."""

    organization_id: uuid.UUID
    end_user_id: uuid.UUID
    session_id: str | None


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What one pass did, on its way to a row."""

    organization_id: uuid.UUID
    end_user_id: uuid.UUID
    session_id: str | None
    outcome: str
    reason: str | None = None
    model_id: uuid.UUID | None = None
    model_name: str | None = None
    transcripts: int = 0
    candidates: int = 0
    inserted: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    rejected: int = 0
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class HealthDay:
    """One day of the memory-health chart."""

    day: datetime
    runs: int = 0
    failures: int = 0
    written: int = 0
    deduped: int = 0
    superseded: int = 0


@dataclass(frozen=True, slots=True)
class MemoryHealth:
    """SPEC §10.1's memory-health block, plus the two rates task 13 adds.

    The rates are properties rather than columns because they are ratios of numbers already
    here, and a stored ratio is a number that can disagree with its own inputs.
    """

    days: tuple[HealthDay, ...] = ()
    runs: int = 0
    failures: int = 0
    written: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    rejected: int = 0
    candidates: int = 0
    #: Live facts across the organization right now, and how many people they are about.
    facts: int = 0
    end_users_with_facts: int = 0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.runs if self.runs else 0.0

    @property
    def dedupe_rate(self) -> float:
        """Of everything the extractor proposed, the share that was already known.

        **Near 1.0 is the alarm.** It means passes are succeeding, costing money, and
        producing nothing — usually a model that has started paraphrasing the conversation
        back, or a dedupe threshold set so low that two different preferences collapse into
        one. Nothing else in the system shows it: the jobs are green and the facts are there.
        """
        return self.deduped / self.candidates if self.candidates else 0.0

    @property
    def supersession_rate(self) -> float:
        """Of everything proposed, the share that replaced something.

        **Near zero on a long-lived user is the other alarm.** People change their minds,
        and a memory that only ever grows is one that is not noticing. It reads as healthy
        because facts are being written; it ends as a prompt holding two contradictory
        sentences with no way to tell which is current.
        """
        return self.superseded / self.candidates if self.candidates else 0.0

    @property
    def average_facts_per_end_user(self) -> float:
        return self.facts / self.end_users_with_facts if self.end_users_with_facts else 0.0


class DistillationTransaction(Protocol):
    """One scoped unit of work for the worker."""

    @property
    def scope(self) -> TenantScope: ...

    async def pending(
        self,
        end_user_id: uuid.UUID,
        session_id: str | None,
        *,
        limit: int = MAX_TRANSCRIPTS_PER_PASS,
    ) -> Sequence[PendingTranscript]:
        """Undistilled transcripts for one thread, oldest first."""
        ...

    async def mark_distilled(
        self, entries: Sequence[PendingTranscript], *, at: datetime
    ) -> int: ...

    async def record(self, run: RunRecord) -> DistillationRun:
        """Write the run row. Returns it, so a caller can log its id."""
        ...

    async def calls_since(self, since: datetime, *, end_user_id: uuid.UUID | None = None) -> int:
        """Runs that reached the model since ``since`` — the cost guard's numerator.

        Skipped runs do not count: refusing to call a model is not calling one, and a cap
        that counted its own refusals would latch on for the rest of the day.
        """
        ...

    async def pending_sessions(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        end_user_id: uuid.UUID | None = None,
    ) -> Sequence[PendingSession]:
        """Threads with undistilled transcripts in a window.

        The backfill's worklist, and — narrowed to one person — what "Distil now" runs over.
        One query rather than two, because the two differ by a ``WHERE`` clause and would
        otherwise differ by more the first time somebody changed one of them.
        """
        ...

    async def health(self, *, start: datetime, end: datetime) -> MemoryHealth: ...

    async def commit(self) -> None: ...


class DistillationStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[DistillationTransaction]: ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class DistillationRunRepository(ScopedRepository[DistillationRun]):
    model = DistillationRun


class PostgresDistillationTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._runs = DistillationRunRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def pending(
        self,
        end_user_id: uuid.UUID,
        session_id: str | None,
        *,
        limit: int = MAX_TRANSCRIPTS_PER_PASS,
    ) -> Sequence[PendingTranscript]:
        statement = (
            select(
                Transcript.request_log_id,
                Transcript.created_at,
                Transcript.request_body,
                Transcript.response_body,
            )
            .join(
                RequestLog,
                (RequestLog.id == Transcript.request_log_id)
                & (RequestLog.created_at == Transcript.created_at),
            )
            .where(
                self._scope.clause(Transcript),
                RequestLog.end_user_id == end_user_id,
                Transcript.distilled_at.is_(None),
                # A failed request has no answer and usually no useful question either;
                # distilling one teaches the assistant about an outage.
                RequestLog.status_code < 400,
                _session_clause(session_id),
            )
            .order_by(Transcript.created_at.asc())
            .limit(limit)
            .execution_options(**scoped())
        )
        rows = (await self._session.execute(statement)).all()
        return [
            PendingTranscript(
                log_id=row[0], created_at=row[1], request_body=row[2], response_body=row[3]
            )
            for row in rows
        ]

    async def mark_distilled(self, entries: Sequence[PendingTranscript], *, at: datetime) -> int:
        if not entries:
            return 0
        total = 0
        for entry in entries:
            statement = (
                update(Transcript)
                .where(
                    self._scope.clause(Transcript),
                    Transcript.request_log_id == entry.log_id,
                    # The partition key comes along: without it PostgreSQL has to probe
                    # every partition for a key that is only unique within one.
                    Transcript.created_at == entry.created_at,
                )
                .values(distilled_at=at)
                .execution_options(**scoped())
            )
            result = await self._session.execute(statement)
            total += int(getattr(result, "rowcount", 0) or 0)
        return total

    async def record(self, run: RunRecord) -> DistillationRun:
        row = _row_of(run)
        self._session.add(row)
        await self._session.flush()
        return row

    async def calls_since(self, since: datetime, *, end_user_id: uuid.UUID | None = None) -> int:
        statement = (
            select(func.count())
            .select_from(DistillationRun)
            .where(
                self._scope.clause(DistillationRun),
                DistillationRun.created_at >= since,
                DistillationRun.outcome.in_((SUCCEEDED, FAILED)),
            )
            .execution_options(**scoped())
        )
        if end_user_id is not None:
            statement = statement.where(DistillationRun.end_user_id == end_user_id)
        return int((await self._session.execute(statement)).scalar() or 0)

    async def pending_sessions(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        end_user_id: uuid.UUID | None = None,
    ) -> Sequence[PendingSession]:
        statement = (
            select(RequestLog.organization_id, RequestLog.end_user_id, RequestLog.session_id)
            .join(
                Transcript,
                (RequestLog.id == Transcript.request_log_id)
                & (RequestLog.created_at == Transcript.created_at),
            )
            .where(
                self._scope.clause(RequestLog),
                RequestLog.created_at >= start,
                RequestLog.created_at < end,
                RequestLog.end_user_id.is_not(None),
                RequestLog.status_code < 400,
                Transcript.distilled_at.is_(None),
            )
            .group_by(RequestLog.organization_id, RequestLog.end_user_id, RequestLog.session_id)
            .order_by(RequestLog.organization_id, RequestLog.end_user_id)
            .limit(limit)
            .execution_options(**scoped())
        )
        if end_user_id is not None:
            statement = statement.where(RequestLog.end_user_id == end_user_id)
        rows = (await self._session.execute(statement)).all()
        return [
            PendingSession(organization_id=row[0], end_user_id=row[1], session_id=row[2])
            for row in rows
        ]

    async def health(self, *, start: datetime, end: datetime) -> MemoryHealth:
        day = func.date_trunc("day", DistillationRun.created_at).label("day")
        statement = (
            select(
                day,
                func.count().label("runs"),
                func.count().filter(DistillationRun.outcome == FAILED).label("failures"),
                func.coalesce(func.sum(DistillationRun.inserted), 0),
                func.coalesce(func.sum(DistillationRun.deduped), 0),
                func.coalesce(func.sum(DistillationRun.superseded), 0),
                func.coalesce(func.sum(DistillationRun.evicted), 0),
                func.coalesce(func.sum(DistillationRun.rejected), 0),
                func.coalesce(func.sum(DistillationRun.candidates), 0),
            )
            .where(
                self._scope.clause(DistillationRun),
                DistillationRun.created_at >= start,
                DistillationRun.created_at < end,
            )
            .group_by(day)
            .order_by(day)
            .execution_options(**scoped())
        )
        rows = (await self._session.execute(statement)).all()

        facts = (
            select(
                func.count(),
                func.count(func.distinct(MemoryFact.end_user_id)),
            )
            .where(self._scope.clause(MemoryFact), MemoryFact.superseded_at.is_(None))
            .execution_options(**scoped())
        )
        totals = (await self._session.execute(facts)).one()
        return _health_of(rows, facts=int(totals[0] or 0), people=int(totals[1] or 0))

    async def commit(self) -> None:
        await self._session.commit()


def _session_clause(session_id: str | None) -> Any:
    """Match one thread, or the rows that have no thread at all.

    ``session_id IS NULL`` cannot be written as ``= NULL``, and a job whose session is null
    — a backfill over transcripts logged before identity existed — must not silently match
    every conversation the person has ever had.
    """
    if session_id is None:
        return RequestLog.session_id.is_(None)
    return RequestLog.session_id == session_id


def _row_of(run: RunRecord) -> DistillationRun:
    return DistillationRun(
        id=uuid7(),
        organization_id=run.organization_id,
        end_user_id=run.end_user_id,
        session_id=run.session_id,
        outcome=run.outcome,
        reason=(run.reason or None) and run.reason[:MAX_REASON_LENGTH],
        model_id=run.model_id,
        model_name=run.model_name,
        transcripts=run.transcripts,
        candidates=run.candidates,
        inserted=run.inserted,
        deduped=run.deduped,
        superseded=run.superseded,
        evicted=run.evicted,
        rejected=run.rejected,
        duration_ms=run.duration_ms,
        created_at=datetime.now(UTC),
    )


def _health_of(rows: Sequence[Any], *, facts: int, people: int) -> MemoryHealth:
    days = tuple(
        HealthDay(
            day=row[0],
            runs=int(row[1]),
            failures=int(row[2]),
            written=int(row[3]),
            deduped=int(row[4]),
            superseded=int(row[5]),
        )
        for row in rows
    )
    return MemoryHealth(
        days=days,
        runs=sum(int(row[1]) for row in rows),
        failures=sum(int(row[2]) for row in rows),
        written=sum(int(row[3]) for row in rows),
        deduped=sum(int(row[4]) for row in rows),
        superseded=sum(int(row[5]) for row in rows),
        evicted=sum(int(row[6]) for row in rows),
        rejected=sum(int(row[7]) for row in rows),
        candidates=sum(int(row[8]) for row in rows),
        facts=facts,
        end_users_with_facts=people,
    )


class PostgresDistillationStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[DistillationTransaction]:
        async with self._session_factory() as session:
            yield PostgresDistillationTransaction(session, scope)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryDistillationTransaction:
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def _pairs(self) -> list[tuple[RequestLog, Transcript]]:
        pairs = []
        for log_id, transcript in self._db.transcripts.items():
            log = self._db.request_logs.get(log_id)
            if log is None or not self._scope.permits(log.organization_id):
                continue
            pairs.append((log, transcript))
        return pairs

    async def pending(
        self,
        end_user_id: uuid.UUID,
        session_id: str | None,
        *,
        limit: int = MAX_TRANSCRIPTS_PER_PASS,
    ) -> Sequence[PendingTranscript]:
        found = [
            PendingTranscript(
                log_id=log.id,
                created_at=transcript.created_at,
                request_body=transcript.request_body,
                response_body=transcript.response_body,
            )
            for log, transcript in self._pairs()
            if log.end_user_id == end_user_id
            and log.session_id == session_id
            and transcript.distilled_at is None
            and log.status_code < 400
        ]
        found.sort(key=lambda entry: (entry.created_at, str(entry.log_id)))
        return found[:limit]

    async def mark_distilled(self, entries: Sequence[PendingTranscript], *, at: datetime) -> int:
        marked = 0
        for entry in entries:
            transcript = self._db.transcripts.get(entry.log_id)
            if transcript is None:
                continue
            transcript.distilled_at = at
            marked += 1
        return marked

    async def record(self, run: RunRecord) -> DistillationRun:
        row = _row_of(run)
        self._db.distillation_runs[row.id] = row
        return row

    async def calls_since(self, since: datetime, *, end_user_id: uuid.UUID | None = None) -> int:
        return sum(
            1
            for row in self._db.distillation_runs.values()
            if self._scope.permits(row.organization_id)
            and row.created_at >= since
            and row.outcome in (SUCCEEDED, FAILED)
            and (end_user_id is None or row.end_user_id == end_user_id)
        )

    async def pending_sessions(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
        end_user_id: uuid.UUID | None = None,
    ) -> Sequence[PendingSession]:
        seen: dict[tuple[uuid.UUID, uuid.UUID, str | None], PendingSession] = {}
        for log, transcript in self._pairs():
            if transcript.distilled_at is not None or log.end_user_id is None:
                continue
            if end_user_id is not None and log.end_user_id != end_user_id:
                continue
            if not (start <= log.created_at < end) or log.status_code >= 400:
                continue
            key = (log.organization_id, log.end_user_id, log.session_id)
            seen.setdefault(
                key,
                PendingSession(
                    organization_id=log.organization_id,
                    end_user_id=log.end_user_id,
                    session_id=log.session_id,
                ),
            )
        return list(seen.values())[:limit]

    async def health(self, *, start: datetime, end: datetime) -> MemoryHealth:
        buckets: dict[datetime, list[int]] = {}
        for row in self._db.distillation_runs.values():
            if not self._scope.permits(row.organization_id):
                continue
            if not (start <= row.created_at < end):
                continue
            day = row.created_at.replace(hour=0, minute=0, second=0, microsecond=0)
            bucket = buckets.setdefault(day, [0, 0, 0, 0, 0, 0, 0, 0])
            bucket[0] += 1
            bucket[1] += 1 if row.outcome == FAILED else 0
            bucket[2] += row.inserted
            bucket[3] += row.deduped
            bucket[4] += row.superseded
            bucket[5] += row.evicted
            bucket[6] += row.rejected
            bucket[7] += row.candidates

        now = datetime.now(UTC)
        live = [
            fact
            for fact in self._db.memory_facts.values()
            if self._scope.permits(fact.organization_id) and is_live(fact, now)
        ]
        rows = [(day, *bucket) for day, bucket in sorted(buckets.items())]
        return _health_of(rows, facts=len(live), people=len({fact.end_user_id for fact in live}))

    async def commit(self) -> None:
        return None


@dataclass
class MemoryDistillationStore:
    database: MemoryDatabase = field(default_factory=MemoryDatabase)

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[DistillationTransaction]:
        yield MemoryDistillationTransaction(self.database, scope)


def start_of_day(now: datetime | None = None) -> datetime:
    """UTC midnight. Where the daily cap resets, stated once so the guard and the screen
    cannot disagree about when 'today' began."""
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def day_window(days: int, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The health chart's window: ``days`` whole days back, up to the next midnight."""
    end = start_of_day(now) + timedelta(days=1)
    return end - timedelta(days=days), end


__all__ = [
    "FAILED",
    "MAX_TRANSCRIPTS_PER_PASS",
    "SKIPPED",
    "SUCCEEDED",
    "DistillationStore",
    "DistillationTransaction",
    "HealthDay",
    "MemoryDistillationStore",
    "MemoryDistillationTransaction",
    "MemoryHealth",
    "PendingSession",
    "PendingTranscript",
    "PostgresDistillationStore",
    "PostgresDistillationTransaction",
    "RunRecord",
    "day_window",
    "start_of_day",
]
