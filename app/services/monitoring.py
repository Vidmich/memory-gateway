"""The monitoring screen's questions, and the rules about how they may be asked.

Everything here is a read. The interesting decisions are about what the API refuses.

**The client never chooses the bucket count.** :func:`choose_interval` maps the requested
window onto a fixed ladder of intervals, and a client-supplied ``interval`` is snapped to
the same ladder and then widened until the bucket count fits. Without that, ``?from=2020&
interval=60`` is a query that reads three million buckets out of a partitioned table and
returns a chart nobody can look at — and the person who asks for it will be a browser
with a stale URL, not an attacker.

**The detail lookup derives its own time window from the id.** ``request_logs`` is
partitioned by day, so a lookup by primary key alone probes every partition retained.
Primary keys here are UUIDv7, which carry the millisecond they were minted, so the id
itself says which day to look in. That is the concrete reason task 01 chose UUIDv7 over
UUIDv4, arriving six tasks later.

**The summary is cached for thirty seconds and the timeseries is not.** The summary is
what the dashboard and every screen refresh asks for, it is the same query for everybody
in an organization, and it is the expensive one — several aggregates over the window. The
timeseries is already bucketed and is asked for once per chart. Caching both would double
the invalidation surface for a saving on the cheaper half.

The cache is keyed by the *whole* query, organization included, and it is read-through
and fail-open: a Redis outage makes the screen slower, never wrong and never blank.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, get_args

from redis.asyncio import Redis

from app.core.errors import NotFound, Validation
from app.core.ids import timestamp_ms_of
from app.core.tenancy import Actor
from app.db.models import RequestLog
from app.services.metrics_store import (
    Bucket,
    ErrorGroup,
    GroupBy,
    LogDetail,
    LogFilters,
    Metric,
    MetricsRepository,
    ModelTraffic,
    Percentiles,
    Summary,
)
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of

logger = logging.getLogger(__name__)

NO_SUCH_LOG = "No such request."

#: The intervals a chart may be bucketed at, coarsest last. A client asking for anything
#: else gets the nearest one at or above it, so there is no way to ask for a resolution
#: that has to be computed rather than chosen.
INTERVALS: tuple[int, ...] = (60, 300, 900, 1800, 3600, 6 * 3600, 24 * 3600)

#: Points per chart. 1000 is already more than a 1200-pixel chart can draw distinctly;
#: past it the response is bigger than the picture.
MAX_BUCKETS = 1000

#: The default ladder, as ``(window, interval)`` pairs: the first window a range fits
#: inside decides its bucket. The task's own examples — an hour at one minute, thirty
#: days at one hour — are the first and last rungs.
LADDER: tuple[tuple[timedelta, int], ...] = (
    (timedelta(hours=2), 60),
    (timedelta(hours=24), 300),
    (timedelta(days=7), 1800),
    (timedelta(days=31), 3600),
)

#: How long a summary answer may be reused. Short enough that "is the error I just caused
#: visible yet" is answered within one impatient refresh.
SUMMARY_CACHE_TTL_SECONDS = 30

#: The furthest back a query may reach. Task 17 enforces retention; until then this keeps
#: an open-ended ``from`` from turning into a full scan of every partition.
MAX_WINDOW = timedelta(days=90)

#: Slack either side of a request's id-derived timestamp when looking one up. A UUIDv7's
#: millisecond is minted in-process and the row's ``created_at`` is the same value, so
#: this only has to absorb the two being written from different clocks.
DETAIL_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Series:
    """A bucketed answer, with the interval the server actually used.

    Returning the interval matters: the client asked for one and may have been given a
    coarser one, and a chart that labels its x-axis from the request rather than from the
    response is a chart that lies about its own resolution.
    """

    interval_seconds: int
    buckets: tuple[Bucket, ...]


class SummaryCache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def put(self, key: str, value: dict[str, Any]) -> None: ...


class RedisSummaryCache:
    """Thirty seconds of memory, and no opinions about correctness.

    Every method swallows its own failures. A summary card is not worth an incident, and
    a monitoring screen that goes blank when Redis restarts is the worst possible moment
    for a monitoring screen to go blank.
    """

    PREFIX = "metrics:summary:"

    def __init__(self, redis: Redis, *, ttl_seconds: int = SUMMARY_CACHE_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._redis.get(f"{self.PREFIX}{key}")
        except Exception:
            logger.warning("metrics cache unavailable; reading through", exc_info=True)
            return None
        if not raw:
            return None
        try:
            decoded: Any = json.loads(raw)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None

    async def put(self, key: str, value: dict[str, Any]) -> None:
        try:
            # `default=str` because the payload carries UUIDs. Serialising them as
            # text is lossless here: they are read back through `_as_uuid`.
            await self._redis.set(
                f"{self.PREFIX}{key}", json.dumps(value, default=str), ex=self._ttl
            )
        except Exception:
            logger.warning("could not cache a metrics summary", exc_info=True)


class MonitoringService:
    def __init__(
        self,
        repository: MetricsRepository,
        *,
        cache: SummaryCache | None = None,
    ) -> None:
        self._repository = repository
        self._cache = cache

    async def summary(self, actor: Actor, filters: LogFilters) -> Summary:
        key = _cache_key(actor, filters)
        cached = await self._cached(key)
        if cached is not None:
            return cached

        async with self._repository.begin(actor.scope) as transaction:
            result = await transaction.summary(filters)

        await self._remember(key, result)
        return result

    async def _cached(self, key: str) -> Summary | None:
        """Read through on any failure.

        The guarantee lives here rather than in one implementation of the cache, because
        it is this service that promises it. A cache that raises is a cache that is
        missing, and a monitoring screen going blank when Redis restarts is the worst
        possible moment for a monitoring screen to go blank.
        """
        if self._cache is None:
            return None
        try:
            payload = await self._cache.get(key)
        except Exception:
            logger.warning("metrics cache read failed; reading through", exc_info=True)
            return None
        return _summary_from(payload) if payload is not None else None

    async def _remember(self, key: str, result: Summary) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.put(key, asdict(result))
        except Exception:
            logger.warning("could not cache a metrics summary", exc_info=True)

    async def timeseries(
        self,
        actor: Actor,
        filters: LogFilters,
        *,
        metric: Metric = "requests",
        group_by: GroupBy = "none",
        interval_seconds: int | None = None,
    ) -> Series:
        interval = choose_interval(filters.start, filters.end, requested=interval_seconds)
        async with self._repository.begin(actor.scope) as transaction:
            buckets = await transaction.timeseries(
                filters, metric=metric, group_by=group_by, interval_seconds=interval
            )
        return Series(interval_seconds=interval, buckets=tuple(buckets))

    async def list_logs(
        self,
        actor: Actor,
        filters: LogFilters,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[RequestLog]:
        size = clamp_limit(limit)
        after = decode_cursor(cursor)
        async with self._repository.begin(actor.scope) as transaction:
            rows = await transaction.logs(filters, after=after, limit=size)
        return page_of(rows, limit=size, cursor_of=lambda row: row.id)

    async def get_log(self, actor: Actor, log_id: uuid.UUID) -> LogDetail:
        """One request, with its transcript if one was stored.

        The window comes from the id, not from the caller — see the module docstring.
        A 404 covers both "no such request" and "another organization's request", the
        same as everywhere else.
        """
        async with self._repository.begin(actor.scope) as transaction:
            detail = await transaction.log(log_id, _window_around(log_id))
        if detail is None:
            raise NotFound(NO_SUCH_LOG)
        return detail


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def build_filters(
    *,
    start: datetime | None,
    end: datetime | None,
    gateway_id: uuid.UUID | None = None,
    upstream_model_id: uuid.UUID | None = None,
    status_class: str | None = None,
    end_user_id: uuid.UUID | None = None,
    session_id: str | None = None,
    streamed: bool | None = None,
    min_latency_ms: int | None = None,
    search: str | None = None,
) -> LogFilters:
    """Turn query parameters into a checked window.

    Defaults to the last 24 hours, which is what the screen opens on. An unbounded
    ``from`` is refused rather than clamped: silently returning ninety days when the
    caller asked for a year produces a chart with the wrong denominator, and nothing on
    it says so.
    """
    now = datetime.now(UTC)
    finish = _aware(end) if end is not None else now
    begin = _aware(start) if start is not None else finish - timedelta(hours=24)

    if finish <= begin:
        raise Validation("'to' has to be after 'from'.", param="to")
    if finish - begin > MAX_WINDOW:
        raise Validation(
            f"The widest window is {MAX_WINDOW.days} days. Narrow the range, or ask for "
            f"the parts separately.",
            param="from",
        )
    if status_class is not None and status_class not in ("2xx", "3xx", "4xx", "5xx"):
        raise Validation("Status class is one of 2xx, 3xx, 4xx, 5xx.", param="status_class")

    return LogFilters(
        start=begin,
        end=finish,
        gateway_id=gateway_id,
        upstream_model_id=upstream_model_id,
        status_class=status_class,
        end_user_id=end_user_id,
        session_id=session_id,
        streamed=streamed,
        min_latency_ms=min_latency_ms,
        # `or None` after the strip, so "   " is the same request as no search at all
        # rather than a filter for the empty string.
        search=(search or "").strip() or None,
    )


def check_metric(metric: str, group_by: str) -> tuple[Metric, GroupBy]:
    """Validate the two enums together, because one constrains the other.

    Grouping only means something for a count. ``latency`` grouped by status class would
    be a p95 over the 5xx requests, which is a number about failures rather than about
    latency, and drawing it next to the overall p95 invites exactly the wrong conclusion.
    """
    if metric not in get_args(Metric.__value__):
        raise Validation(
            f"Metric is one of {', '.join(get_args(Metric.__value__))}.", param="metric"
        )
    if group_by not in get_args(GroupBy.__value__):
        raise Validation(
            f"Group by is one of {', '.join(get_args(GroupBy.__value__))}.", param="group_by"
        )
    if group_by != "none" and metric != "requests":
        raise Validation(
            "Only the request count can be grouped. Latency and token series are already "
            "several lines each.",
            param="group_by",
        )
    return metric, group_by  # type: ignore[return-value]


def choose_interval(start: datetime, end: datetime, *, requested: int | None = None) -> int:
    """The bucket width to use, in seconds.

    Server-side by design (the work item's words), so the client never fetches raw rows
    to aggregate them itself. With no ``requested`` value the ladder decides from the
    range alone. A requested interval is snapped up to the nearest allowed one and then
    widened until the bucket count fits, which is what makes it safe to honour at all:
    the bound is on the answer's size, not on the caller's good manners.
    """
    span = end - start
    interval = _snap(requested) if requested is not None else _default_interval(span)
    # Widening rather than refusing: the caller gets a chart of the range they asked for,
    # drawn at a resolution the response can carry, and `Series.interval_seconds` tells
    # them which one it is.
    while span.total_seconds() / interval > MAX_BUCKETS and interval < INTERVALS[-1]:
        interval = next(step for step in INTERVALS if step > interval)
    return interval


def _default_interval(span: timedelta) -> int:
    for window, interval in LADDER:
        if span <= window:
            return interval
    return INTERVALS[-1]


def _snap(seconds: int) -> int:
    """The first allowed interval at or above ``seconds``."""
    for step in INTERVALS:
        if seconds <= step:
            return step
    return INTERVALS[-1]


def _window_around(log_id: uuid.UUID) -> LogFilters:
    try:
        minted = datetime.fromtimestamp(timestamp_ms_of(log_id) / 1000, tz=UTC)
    except ValueError as exc:
        # Not a UUIDv7, so it was never minted by this system and no row can match it.
        raise NotFound(NO_SUCH_LOG) from exc
    return LogFilters(start=minted - DETAIL_WINDOW, end=minted + DETAIL_WINDOW)


def _aware(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Query strings routinely arrive without a zone, and the alternative — refusing them —
    would make every hand-typed URL a 422 for a value the server can interpret exactly
    one sensible way.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _cache_key(actor: Actor, filters: LogFilters) -> str:
    """A stable digest of everything that changes the answer.

    The organization is *in* the key, not merely implied by it. A cache keyed on the
    filters alone would serve one tenant's totals to another the moment two of them
    picked the same time range, which is the kind of bug that is invisible until it is
    catastrophic.
    """
    payload = json.dumps(
        {
            "organization_id": str(actor.scope.organization_id),
            **{
                key: (value.isoformat() if isinstance(value, datetime) else str(value))
                for key, value in asdict(filters).items()
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _summary_from(payload: dict[str, Any]) -> Summary:
    """Rebuild a cached summary. A payload this build cannot read is treated as a miss."""

    def percentiles(value: Any) -> Percentiles:
        return Percentiles(**value) if isinstance(value, dict) else Percentiles()

    models: Sequence[Any] = payload.get("models") or ()
    errors: Sequence[Any] = payload.get("error_groups") or ()
    return Summary(
        requests=int(payload.get("requests", 0)),
        errors=int(payload.get("errors", 0)),
        status_classes=dict(payload.get("status_classes") or {}),
        total=percentiles(payload.get("total")),
        ttft=percentiles(payload.get("ttft")),
        retrieval=percentiles(payload.get("retrieval")),
        prompt_tokens=int(payload.get("prompt_tokens", 0)),
        completion_tokens=int(payload.get("completion_tokens", 0)),
        memory_tokens=int(payload.get("memory_tokens", 0)),
        models=tuple(
            ModelTraffic(
                upstream_model_id=_as_uuid(item.get("upstream_model_id")),
                model_name=item.get("model_name"),
                requests=int(item.get("requests", 0)),
            )
            for item in models
        ),
        error_groups=tuple(
            ErrorGroup(
                error_code=str(item.get("error_code")), requests=int(item.get("requests", 0))
            )
            for item in errors
        ),
    )


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


__all__ = [
    "INTERVALS",
    "MAX_BUCKETS",
    "MAX_WINDOW",
    "SUMMARY_CACHE_TTL_SECONDS",
    "MonitoringService",
    "RedisSummaryCache",
    "Series",
    "SummaryCache",
    "build_filters",
    "check_metric",
    "choose_interval",
]
