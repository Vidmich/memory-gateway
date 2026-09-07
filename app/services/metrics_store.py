"""Reading the request log: the metadata list, one request's detail, and the aggregates.

This is the seam the task notes ask for. If log volume outgrows PostgreSQL, everything
that has to change lives behind :class:`MetricsRepository` — a ClickHouse implementation
is a third class in this file and no caller notices. That is only true because nothing
outside this module writes SQL against ``request_logs``, and because the aggregates are
computed *in the store* rather than by fetching rows and summing them in Python. An
endpoint that pulled a million rows to count them would be a rewrite, not a swap.

**Percentiles are computed in the database.** ``percentile_disc`` over the partition
range, in one query with the counts and sums, so the summary card is a single round trip.
The in-memory implementation reproduces PostgreSQL's *discrete* definition exactly —
the smallest value whose position in the ordering reaches the fraction, which for four
samples and p50 is the second one, not the average of the second and third. The contract
test asserts both against the same fixture, so a divergence is a failure rather than a
difference nobody notices until a chart disagrees with a support ticket.

**Time filters are always present.** Every query here takes a closed ``[from, to)``
window, and the window is what lets PostgreSQL prune partitions. A read with no bound
would scan every day retained; there is deliberately no way to ask for one.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy import ColumnElement, DateTime, Select, and_, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import INTERVAL, array
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import RequestLog, Transcript
from app.db.scoping import ScopedRepository, scoped
from app.services.memory_db import MemoryDatabase

#: The fractions every latency card reports. Fixed rather than configurable: SPEC §10.1
#: names these three, and a chart whose percentiles move between deployments cannot be
#: compared with last week's screenshot.
PERCENTILES = (0.5, 0.95, 0.99)

type Metric = Literal["requests", "latency", "tokens"]
type GroupBy = Literal["none", "status_class", "model", "gateway"]


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogFilters:
    """What the monitoring screen is asking about.

    ``start`` and ``end`` are required and everything else is optional, which is the
    shape of the screen: a time range is always chosen, and the filters narrow it.
    """

    start: datetime
    end: datetime
    gateway_id: uuid.UUID | None = None
    upstream_model_id: uuid.UUID | None = None
    #: ``2xx``, ``4xx`` or ``5xx``. A class rather than an exact code because that is the
    #: question people ask, and because a filter for "any failure" should not need seven.
    status_class: str | None = None
    end_user_id: uuid.UUID | None = None
    session_id: str | None = None
    streamed: bool | None = None
    min_latency_ms: int | None = None
    #: Free text over the error code and message. Only over the error: searching prompt
    #: bodies would mean scanning the large table the split exists to avoid, and would
    #: quietly turn the monitoring screen into a content-search tool over end-user data.
    search: str | None = None


@dataclass(frozen=True, slots=True)
class Percentiles:
    """``None`` throughout when no row in the window carried the measurement."""

    p50: int | None = None
    p95: int | None = None
    p99: int | None = None

    @classmethod
    def of(cls, values: Sequence[int | None] | None) -> Percentiles:
        if not values:
            return cls()
        return cls(p50=values[0], p95=values[1], p99=values[2])


@dataclass(frozen=True, slots=True)
class ModelTraffic:
    """One row of the "traffic distribution across upstream targets" chart.

    Keyed by name as well as id, because a model that has been deleted still has traffic
    in the window and a chart segment labelled with a bare UUID helps nobody.
    """

    upstream_model_id: uuid.UUID | None
    model_name: str | None
    requests: int


@dataclass(frozen=True, slots=True)
class ErrorGroup:
    """One bar of the error taxonomy: the gateway's own code, not the HTTP status."""

    error_code: str
    requests: int


@dataclass(frozen=True, slots=True)
class Summary:
    """Everything the summary cards and the distribution charts need, in one read."""

    requests: int = 0
    errors: int = 0
    #: ``{"2xx": 900, "4xx": 80, "5xx": 20}``, absent classes omitted.
    status_classes: dict[str, int] = field(default_factory=dict)
    total: Percentiles = field(default_factory=Percentiles)
    ttft: Percentiles = field(default_factory=Percentiles)
    retrieval: Percentiles = field(default_factory=Percentiles)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    memory_tokens: int = 0
    models: tuple[ModelTraffic, ...] = ()
    error_groups: tuple[ErrorGroup, ...] = ()

    @property
    def error_rate(self) -> float:
        return (self.errors / self.requests) if self.requests else 0.0


@dataclass(frozen=True, slots=True)
class Bucket:
    """One point on the x-axis, with every series' value at it.

    Series are a mapping rather than a single number because a metric is usually several
    lines on one chart — latency is p50/p95/p99, tokens are prompt/completion/memory —
    and issuing one request per line would multiply the load on the aggregation queries
    by exactly the number of lines nobody wanted separately.
    """

    start: datetime
    series: dict[str, float]


@dataclass(frozen=True, slots=True)
class LogDetail:
    """A metadata row and its transcript, if one was stored and still exists."""

    log: RequestLog
    transcript: Transcript | None


# ---------------------------------------------------------------------------
# the port
# ---------------------------------------------------------------------------


class MetricsTransaction(Protocol):
    """One scoped read. Every method is bounded by ``filters.start`` / ``filters.end``."""

    async def summary(self, filters: LogFilters) -> Summary: ...

    async def timeseries(
        self,
        filters: LogFilters,
        *,
        metric: Metric,
        group_by: GroupBy,
        interval_seconds: int,
    ) -> Sequence[Bucket]: ...

    async def logs(
        self, filters: LogFilters, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[RequestLog]:
        """Newest first, over-fetched by one so the caller can page."""

    async def log(self, log_id: uuid.UUID, filters: LogFilters) -> LogDetail | None:
        """One request, or ``None`` — including when it belongs to another organization."""


class MetricsRepository(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[MetricsTransaction]: ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class RequestLogRepository(ScopedRepository[RequestLog]):
    """Scoped reads over the metadata table.

    Every statement built here carries the time window as well as the scope. That is not
    only about partition pruning: an unbounded read of a tenant's whole retained history
    is a query that gets slower every day it runs, and the only good moment to make it
    impossible is before anybody writes one.
    """

    model = RequestLog

    def window(self, filters: LogFilters) -> Select[tuple[RequestLog]]:
        return self.select().where(*_conditions(filters))


class PostgresMetricsTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._logs = RequestLogRepository(session, scope)

    async def summary(self, filters: LogFilters) -> Summary:
        where = [self._scope.clause(RequestLog), *_conditions(filters)]

        totals = (
            await self._session.execute(
                select(
                    func.count().label("requests"),
                    func.count().filter(RequestLog.status_code >= 400).label("errors"),
                    func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                    func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                    func.coalesce(func.sum(RequestLog.memory_tokens), 0),
                    _percentiles(RequestLog.latency_total_ms),
                    _percentiles(RequestLog.latency_ttft_ms),
                    _percentiles(RequestLog.latency_retrieval_ms),
                )
                .where(*where)
                .execution_options(**scoped())
            )
        ).one()

        classes = (
            await self._session.execute(
                select(_status_class(), func.count())
                .where(*where)
                .group_by(_status_class())
                .execution_options(**scoped())
            )
        ).all()

        models = (
            await self._session.execute(
                select(RequestLog.upstream_model_id, RequestLog.model_name, func.count())
                .where(*where)
                .group_by(RequestLog.upstream_model_id, RequestLog.model_name)
                .order_by(func.count().desc())
                .execution_options(**scoped())
            )
        ).all()

        errors = (
            await self._session.execute(
                select(RequestLog.error_code, func.count())
                .where(*where, RequestLog.error_code.is_not(None))
                .group_by(RequestLog.error_code)
                .order_by(func.count().desc())
                .execution_options(**scoped())
            )
        ).all()

        return Summary(
            requests=totals[0],
            errors=totals[1],
            status_classes={str(row[0]): row[1] for row in classes},
            prompt_tokens=totals[2],
            completion_tokens=totals[3],
            memory_tokens=totals[4],
            total=Percentiles.of(totals[5]),
            ttft=Percentiles.of(totals[6]),
            retrieval=Percentiles.of(totals[7]),
            models=tuple(
                ModelTraffic(upstream_model_id=row[0], model_name=row[1], requests=row[2])
                for row in models
            ),
            error_groups=tuple(
                ErrorGroup(error_code=str(row[0]), requests=row[1]) for row in errors
            ),
        )

    async def timeseries(
        self,
        filters: LogFilters,
        *,
        metric: Metric,
        group_by: GroupBy,
        interval_seconds: int,
    ) -> Sequence[Bucket]:
        # `date_bin` anchors buckets to the epoch rather than to the window's start, so
        # the same minute is the same bucket in every query — two charts drawn from two
        # requests line up, and the 30-second cache cannot serve one that is offset.
        bucket = func.date_bin(
            cast(literal(f"{interval_seconds} seconds"), INTERVAL),
            RequestLog.created_at,
            cast(literal(EPOCH), DateTime(timezone=True)),
        ).label("bucket")

        columns: list[Any] = [bucket]
        grouping = _grouping_column(group_by)
        if grouping is not None:
            columns.append(grouping.label("grouping"))
        columns.extend(_metric_columns(metric))

        group_terms: list[Any] = [bucket] if grouping is None else [bucket, grouping]
        rows = (
            await self._session.execute(
                select(*columns)
                .where(self._scope.clause(RequestLog), *_conditions(filters))
                .group_by(*group_terms)
                .order_by(bucket)
                .execution_options(**scoped())
            )
        ).all()

        return _assemble(rows, metric=metric, grouped=grouping is not None)

    async def logs(
        self, filters: LogFilters, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[RequestLog]:
        statement = self._logs.window(filters).order_by(RequestLog.id.desc()).limit(limit + 1)
        if after is not None:
            statement = statement.where(RequestLog.id < after)
        return (await self._session.execute(statement)).scalars().all()

    async def log(self, log_id: uuid.UUID, filters: LogFilters) -> LogDetail | None:
        # The window comes along even for a lookup by id: without it PostgreSQL has to
        # probe every partition, because a primary key on a partitioned table is only
        # unique within a partition and the planner cannot know which one holds this id.
        row = (
            (await self._session.execute(self._logs.window(filters).where(RequestLog.id == log_id)))
            .scalars()
            .first()
        )
        if row is None:
            return None

        transcript = (
            (
                await self._session.execute(
                    select(Transcript)
                    .where(
                        Transcript.request_log_id == log_id,
                        Transcript.created_at == row.created_at,
                        self._scope.clause(Transcript),
                    )
                    .execution_options(**scoped())
                )
            )
            .scalars()
            .first()
        )
        return LogDetail(log=row, transcript=transcript)


class PostgresMetricsRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[MetricsTransaction]:
        async with self._session_factory() as session:
            yield PostgresMetricsTransaction(session, scope)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryMetricsTransaction:
    """The same answers, computed in Python over the same rows.

    It exists because the aggregation is the part most likely to be quietly wrong, and
    because no PostgreSQL is reachable from a laptop with the stack down. The contract
    test drives both through one fixture whose percentiles were worked out by hand.
    """

    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    def _rows(self, filters: LogFilters) -> list[RequestLog]:
        return [row for row in self._db.request_logs.values() if self._matches(row, filters)]

    def _matches(self, row: RequestLog, filters: LogFilters) -> bool:
        if not self._scope.permits(row.organization_id):
            return False
        if not (filters.start <= _aware(row.created_at) < filters.end):
            return False
        if filters.gateway_id is not None and row.gateway_id != filters.gateway_id:
            return False
        if (
            filters.upstream_model_id is not None
            and row.upstream_model_id != filters.upstream_model_id
        ):
            return False
        wanted_class = filters.status_class
        if wanted_class is not None and status_class(row.status_code) != wanted_class:
            return False
        if filters.end_user_id is not None and row.end_user_id != filters.end_user_id:
            return False
        if filters.session_id is not None and row.session_id != filters.session_id:
            return False
        if filters.streamed is not None and bool(row.streamed) != filters.streamed:
            return False
        if filters.min_latency_ms is not None and row.latency_total_ms < filters.min_latency_ms:
            return False
        if filters.search:
            needle = filters.search.lower()
            haystack = f"{row.error_code or ''} {row.error_message or ''}".lower()
            if needle not in haystack:
                return False
        return True

    async def summary(self, filters: LogFilters) -> Summary:
        rows = self._rows(filters)
        classes: dict[str, int] = {}
        models: dict[tuple[uuid.UUID | None, str | None], int] = {}
        errors: dict[str, int] = {}
        for row in rows:
            key = status_class(row.status_code)
            classes[key] = classes.get(key, 0) + 1
            model_key = (row.upstream_model_id, row.model_name)
            models[model_key] = models.get(model_key, 0) + 1
            if row.error_code:
                errors[row.error_code] = errors.get(row.error_code, 0) + 1

        return Summary(
            requests=len(rows),
            errors=sum(1 for row in rows if row.status_code >= 400),
            status_classes=classes,
            prompt_tokens=sum(row.prompt_tokens or 0 for row in rows),
            completion_tokens=sum(row.completion_tokens or 0 for row in rows),
            memory_tokens=sum(row.memory_tokens or 0 for row in rows),
            total=percentiles([row.latency_total_ms for row in rows]),
            ttft=percentiles([row.latency_ttft_ms for row in rows]),
            retrieval=percentiles([row.latency_retrieval_ms for row in rows]),
            models=tuple(
                ModelTraffic(upstream_model_id=key[0], model_name=key[1], requests=count)
                for key, count in sorted(models.items(), key=lambda item: -item[1])
            ),
            error_groups=tuple(
                ErrorGroup(error_code=code, requests=count)
                for code, count in sorted(errors.items(), key=lambda item: -item[1])
            ),
        )

    async def timeseries(
        self,
        filters: LogFilters,
        *,
        metric: Metric,
        group_by: GroupBy,
        interval_seconds: int,
    ) -> Sequence[Bucket]:
        grouped: dict[tuple[datetime, str | None], list[RequestLog]] = {}
        for row in self._rows(filters):
            key = (
                bucket_start(_aware(row.created_at), interval_seconds),
                _group_key(row, group_by),
            )
            grouped.setdefault(key, []).append(row)

        buckets: dict[datetime, dict[str, float]] = {}
        for (start, group), rows in sorted(grouped.items(), key=lambda item: item[0][0]):
            series = buckets.setdefault(start, {})
            for name, value in _series_values(metric, rows).items():
                series[f"{group}.{name}" if group is not None else name] = value
        return [Bucket(start=start, series=series) for start, series in sorted(buckets.items())]

    async def logs(
        self, filters: LogFilters, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[RequestLog]:
        rows = sorted(self._rows(filters), key=lambda row: row.id, reverse=True)
        if after is not None:
            rows = [row for row in rows if row.id < after]
        return rows[: limit + 1]

    async def log(self, log_id: uuid.UUID, filters: LogFilters) -> LogDetail | None:
        row = self._db.request_logs.get(log_id)
        if row is None or not self._matches(row, filters):
            return None
        return LogDetail(log=row, transcript=self._db.transcripts.get(log_id))


class MemoryMetricsRepository:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[MetricsTransaction]:
        yield MemoryMetricsTransaction(self._db, scope)


# ---------------------------------------------------------------------------
# shared arithmetic
# ---------------------------------------------------------------------------


def status_class(status_code: int) -> str:
    """``503`` becomes ``5xx``. One function so SQL and Python cannot disagree."""
    return f"{status_code // 100}xx"


#: What ``date_bin`` is anchored to. Buckets are aligned to the epoch rather than to
#: the requested window, so the same minute is the same bucket in every query — two
#: charts drawn from two requests line up, and a cached response cannot be half a bucket
#: out of step with a fresh one.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_start(moment: datetime, interval_seconds: int) -> datetime:
    """The bucket a timestamp falls in, anchored to the epoch like ``date_bin``."""
    elapsed = int((moment - EPOCH).total_seconds())
    return EPOCH + timedelta(seconds=elapsed - elapsed % interval_seconds)


def percentiles(values: Iterable[int | None]) -> Percentiles:
    """PostgreSQL's ``percentile_disc``, in Python.

    *Discrete*: the answer is always a value that actually occurred, namely the first one
    whose position in the ordering reaches the fraction. ``percentile_cont`` would
    interpolate and report a latency nothing ever took, which reads as a bug the first
    time somebody looks for the matching request.
    """
    present = sorted(value for value in values if value is not None)
    if not present:
        return Percentiles()
    return Percentiles(*(present[_index(fraction, len(present))] for fraction in PERCENTILES))


def _index(fraction: float, count: int) -> int:
    return min(max(math.ceil(fraction * count) - 1, 0), count - 1)


def _series_values(metric: Metric, rows: Sequence[RequestLog]) -> dict[str, float]:
    if metric == "requests":
        return {"requests": float(len(rows))}
    if metric == "tokens":
        return {
            "prompt": float(sum(row.prompt_tokens or 0 for row in rows)),
            "completion": float(sum(row.completion_tokens or 0 for row in rows)),
            "memory": float(sum(row.memory_tokens or 0 for row in rows)),
        }
    total = percentiles([row.latency_total_ms for row in rows])
    ttft = percentiles([row.latency_ttft_ms for row in rows])
    retrieval = percentiles([row.latency_retrieval_ms for row in rows])
    return {
        name: float(value)
        for name, value in (
            ("total_p50", total.p50),
            ("total_p95", total.p95),
            ("total_p99", total.p99),
            ("ttft_p95", ttft.p95),
            ("retrieval_p95", retrieval.p95),
        )
        if value is not None
    }


def _group_key(row: RequestLog, group_by: GroupBy) -> str | None:
    if group_by == "status_class":
        return status_class(row.status_code)
    if group_by == "model":
        return row.model_name or "(unknown)"
    if group_by == "gateway":
        # The id, not the name: the gateways screen already knows the names, and a name
        # is not unique enough to key a column by.
        return str(row.gateway_id)
    return None


def _aware(moment: datetime) -> datetime:
    """Timestamps from PostgreSQL carry a zone; ones built in a test may not."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------


def _conditions(filters: LogFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        RequestLog.created_at >= filters.start,
        RequestLog.created_at < filters.end,
    ]
    if filters.gateway_id is not None:
        conditions.append(RequestLog.gateway_id == filters.gateway_id)
    if filters.upstream_model_id is not None:
        conditions.append(RequestLog.upstream_model_id == filters.upstream_model_id)
    if filters.status_class is not None:
        low = int(filters.status_class[0]) * 100
        conditions.append(and_(RequestLog.status_code >= low, RequestLog.status_code < low + 100))
    if filters.end_user_id is not None:
        conditions.append(RequestLog.end_user_id == filters.end_user_id)
    if filters.session_id is not None:
        conditions.append(RequestLog.session_id == filters.session_id)
    if filters.streamed is not None:
        conditions.append(RequestLog.streamed.is_(filters.streamed))
    if filters.min_latency_ms is not None:
        conditions.append(RequestLog.latency_total_ms >= filters.min_latency_ms)
    if filters.search:
        needle = f"%{filters.search}%"
        conditions.append(
            or_(RequestLog.error_code.ilike(needle), RequestLog.error_message.ilike(needle))
        )
    return conditions


def _percentiles(column: Any) -> Any:
    """``percentile_disc(ARRAY[...]) WITHIN GROUP (ORDER BY column)``, nulls excluded.

    The ``FILTER`` is what keeps a column that is null for most rows — ``latency_ttft_ms``
    on a mostly non-streaming gateway — from reporting the percentiles of a set that is
    mostly nothing.
    """
    return (
        func.percentile_disc(array(PERCENTILES))
        .within_group(column.asc())
        .filter(column.is_not(None))
    )


def _status_class() -> Any:
    """``503`` -> ``'5xx'``, in SQL. Integer division, so no rounding to argue about."""
    return func.concat(RequestLog.status_code / 100, "xx")


def _grouping_column(group_by: GroupBy) -> Any:
    if group_by == "status_class":
        return _status_class()
    if group_by == "model":
        return func.coalesce(RequestLog.model_name, "(unknown)")
    if group_by == "gateway":
        return RequestLog.gateway_id
    return None


def _metric_columns(metric: Metric) -> list[Any]:
    if metric == "requests":
        return [func.count()]
    if metric == "tokens":
        return [
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0),
            func.coalesce(func.sum(RequestLog.memory_tokens), 0),
        ]
    return [
        _percentiles(RequestLog.latency_total_ms),
        _percentiles(RequestLog.latency_ttft_ms),
        _percentiles(RequestLog.latency_retrieval_ms),
    ]


def _assemble(rows: Sequence[Any], *, metric: Metric, grouped: bool) -> list[Bucket]:
    """Turn ``(bucket[, grouping], values...)`` rows into one entry per bucket."""
    buckets: dict[datetime, dict[str, float]] = {}
    for row in rows:
        start = _aware(row[0])
        group = str(row[1]) if grouped else None
        values = row[2:] if grouped else row[1:]
        series = buckets.setdefault(start, {})
        for name, value in _named(metric, values).items():
            series[f"{group}.{name}" if group is not None else name] = value
    return [Bucket(start=start, series=series) for start, series in sorted(buckets.items())]


def _named(metric: Metric, values: Sequence[Any]) -> dict[str, float]:
    if metric == "requests":
        return {"requests": float(values[0])}
    if metric == "tokens":
        return {
            "prompt": float(values[0]),
            "completion": float(values[1]),
            "memory": float(values[2]),
        }
    total, ttft, retrieval = (Percentiles.of(value) for value in values[:3])
    return {
        name: float(value)
        for name, value in (
            ("total_p50", total.p50),
            ("total_p95", total.p95),
            ("total_p99", total.p99),
            ("ttft_p95", ttft.p95),
            ("retrieval_p95", retrieval.p95),
        )
        if value is not None
    }


__all__ = [
    "EPOCH",
    "PERCENTILES",
    "Bucket",
    "ErrorGroup",
    "GroupBy",
    "LogDetail",
    "LogFilters",
    "MemoryMetricsRepository",
    "Metric",
    "MetricsRepository",
    "MetricsTransaction",
    "ModelTraffic",
    "Percentiles",
    "PostgresMetricsRepository",
    "RequestLogRepository",
    "Summary",
    "bucket_start",
    "percentiles",
    "status_class",
]
