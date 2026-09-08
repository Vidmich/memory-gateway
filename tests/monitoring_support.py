"""Builders for the request-log and monitoring tests.

The flusher here is deliberately **not started**. A background task on a half-second
timer would make every assertion in this area a race, and the fix — sleeping until it
probably ran — is how a suite ends up slow and flaky at the same time. Instead
:meth:`LogFixture.flush` drains and writes synchronously, so a test says "now everything
is written" and means it. The timer itself is tested on its own, once, in
``tests/test_request_log.py``.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import CollectorRegistry

from app.core.ids import uuid7
from app.core.metrics import LogMetrics, ProxyMetrics, build_log_metrics, build_proxy_metrics
from app.db.models import EndUser, Organization, RequestLog, Transcript
from app.services.log_store import MemoryLogWriter
from app.services.memory_db import MemoryDatabase
from app.services.metrics_store import MemoryMetricsRepository
from app.services.monitoring import MonitoringService, SummaryCache
from app.services.redaction import DEFAULT_BUDGET_SECONDS
from app.services.request_log import LogFlusher, LogQueue, RequestLogService, RequestRecord

#: A fixed "now" for the aggregation fixtures. Percentile assertions are about which
#: value comes back, so the timestamps have to be stable or the window drifts under them.
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


@dataclass
class LogFixture:
    """The write path, wired to memory, with the timer under the test's control."""

    service: RequestLogService
    queue: LogQueue
    flusher: LogFlusher
    writer: MemoryLogWriter
    database: MemoryDatabase
    metrics: LogMetrics
    #: Task 18's data-plane counters, on the same registry, so a test can assert on the
    #: overhead histogram — SPEC §4.2's budget, measured per request — without a whole
    #: application around it.
    proxy: ProxyMetrics
    registry: CollectorRegistry

    async def flush(self) -> int:
        return await self.service.flush_pending()

    @property
    def rows(self) -> list[RequestLog]:
        return sorted(self.database.request_logs.values(), key=lambda row: row.id)

    def transcript(self, log_id: uuid.UUID) -> Transcript | None:
        return self.database.transcripts.get(log_id)

    def counter(self, reason: str) -> float:
        value = self.registry.get_sample_value("logs_dropped_total", {"reason": reason})
        return value or 0.0

    def written(self) -> float:
        return self.registry.get_sample_value("logs_written_total") or 0.0


def build_logs(
    *,
    database: MemoryDatabase | None = None,
    maxsize: int = 1000,
    shed_fraction: float = 0.7,
    redaction_budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    subscriber: Any | None = None,
) -> LogFixture:
    """The write path over memory.

    ``subscriber`` is task 13's hook — what the flusher calls once a batch of transcripts
    is committed. ``None`` is the ordinary case here and in a worker process: conversation
    memory is optional wiring, not a dependency of logging.
    """
    registry = CollectorRegistry()
    metrics = build_log_metrics(registry)
    proxy = build_proxy_metrics(registry)
    writer = MemoryLogWriter(database or MemoryDatabase())
    queue = LogQueue(metrics=metrics, maxsize=maxsize, shed_fraction=shed_fraction)
    flusher = LogFlusher(
        queue,
        writer,
        metrics=metrics,
        redaction_budget_seconds=redaction_budget_seconds,
        subscriber=subscriber,
    )
    return LogFixture(
        service=RequestLogService(queue, flusher, metrics=proxy),
        queue=queue,
        flusher=flusher,
        writer=writer,
        database=writer.database,
        metrics=metrics,
        proxy=proxy,
        registry=registry,
    )


class FakeSummaryCache:
    """A dictionary with the cache's interface, and a count of how often it answered."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.puts = 0

    async def get(self, key: str) -> dict[str, Any] | None:
        found = self.values.get(key)
        if found is not None:
            self.hits += 1
        return found

    async def put(self, key: str, value: dict[str, Any]) -> None:
        self.puts += 1
        self.values[key] = value


class BrokenSummaryCache:
    """Fails both ways. The monitoring service has to read through it, not fall over."""

    async def get(self, key: str) -> dict[str, Any] | None:
        raise RuntimeError("redis is down")

    async def put(self, key: str, value: dict[str, Any]) -> None:
        raise RuntimeError("redis is down")


def build_monitoring(
    database: MemoryDatabase, *, cache: SummaryCache | None = None
) -> MonitoringService:
    return MonitoringService(MemoryMetricsRepository(database), cache=cache)


def make_log_row(
    organization: Organization | uuid.UUID,
    *,
    gateway_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    status_code: int = 200,
    latency_total_ms: int = 100,
    **overrides: Any,
) -> RequestLog:
    """A ``request_logs`` row with sensible defaults, for the read-side tests.

    Built directly rather than through the write path on purpose: the aggregation tests
    are about arithmetic over known values, and going through the recorder would make the
    inputs whatever the clock happened to measure.
    """
    organization_id = organization if isinstance(organization, uuid.UUID) else organization.id
    when = created_at or NOW
    values: dict[str, Any] = {
        # The id's own timestamp is set from `created_at`, because in production the two
        # are minted microseconds apart and the detail lookup depends on it: a UUIDv7 is
        # what tells the server which day's partition to look in. A fixture whose id said
        # "now" and whose row said "last Tuesday" would be testing a state that cannot
        # occur, and failing for it.
        "id": id_at(when),
        "created_at": when,
        "organization_id": organization_id,
        "gateway_id": gateway_id or uuid7(),
        "status_code": status_code,
        "streamed": False,
        "latency_total_ms": latency_total_ms,
        "retrieved_chunk_ids": [],
        "retrieved_fact_ids": [],
        "failover_attempts": [],
        "response_truncated": False,
    }
    values.update(overrides)
    return RequestLog(**values)


#: Strictly increasing, so ids minted for the same instant still sort in creation order.
#: Reusing `uuid7`'s own tail is not good enough: its sub-millisecond counter resets
#: every millisecond, so two rows stamped with the same *fixture* timestamp but minted a
#: millisecond apart would tie on the counter and fall back to random bits — which makes
#: the cursor-paging checks pass or fail depending on how fast the machine is.
_sequence = itertools.count(1)


def id_at(moment: datetime) -> uuid.UUID:
    """A UUIDv7 whose embedded millisecond is ``moment``.

    Version and variant are set as RFC 9562 requires, so this is a real v7 and
    :func:`app.core.ids.timestamp_ms_of` reads it — which is what the detail lookup does.
    """
    epoch_ms = int(moment.timestamp() * 1000)
    value = (epoch_ms & 0xFFFF_FFFF_FFFF) << 80  # unix_ts_ms
    value |= 0x7 << 76  # version
    value |= 0b10 << 62  # variant
    value |= next(_sequence) & ((1 << 62) - 1)
    return uuid.UUID(int=value)


def seed(
    database: MemoryDatabase,
    organization: Organization | uuid.UUID,
    *,
    latencies: tuple[int, ...] = (),
    gateway_id: uuid.UUID | None = None,
    spread_seconds: int = 0,
    **overrides: Any,
) -> list[RequestLog]:
    """One row per latency, optionally spread backwards in time from :data:`NOW`."""
    rows = []
    for index, latency in enumerate(latencies):
        row = make_log_row(
            organization,
            gateway_id=gateway_id,
            latency_total_ms=latency,
            created_at=NOW - timedelta(seconds=index * spread_seconds),
            **overrides,
        )
        database.request_logs[row.id] = row
        rows.append(row)
    return rows


def submitted(fixture: LogFixture) -> list[RequestRecord]:
    """Records still sitting in the queue, for asserting on the drop policy."""
    return fixture.queue.drain_now()


# ---------------------------------------------------------------------------
# the aggregation fixture
# ---------------------------------------------------------------------------
#
# Shared by ``tests/test_metrics_store_memory.py`` and ``tests/test_metrics_db.py`` so the
# two implementations are asked about literally the same rows. The numbers are chosen so
# every expected answer in ``tests/metrics_store_contract.py`` is arithmetic somebody can
# check by reading — twenty latencies five apart, three streamed requests ten minutes
# apart, one of each status class.


#: Where the rate-limit rejections live: three and a half hours past :data:`NOW`, well
#: outside the window every other check uses. Task 14's rows would otherwise change the
#: request counts, the percentiles and the error taxonomy that task 07's checks assert
#: exact numbers for — and a fixture that has to be renumbered every time a task adds a
#: row is a fixture nobody will keep correct.
THROTTLED_AT = NOW + timedelta(hours=3, minutes=30)


@dataclass(frozen=True)
class MetricsSeed:
    """Rows to insert, and the ids the contract checks refer to."""

    logs: tuple[RequestLog, ...]
    transcripts: tuple[Transcript, ...]
    #: Task 14's "top throttled end users" needs rows to join to. Two of Acme's, so the
    #: ordering is a real ordering, plus one of Globex's so the scoping is testable.
    end_users: tuple[EndUser, ...]
    acme_gateway_id: uuid.UUID
    other_gateway_id: uuid.UUID
    globex_gateway_id: uuid.UUID
    acme_log_id: uuid.UUID
    bodiless_log_id: uuid.UUID
    globex_log_id: uuid.UUID
    noisy_end_user_id: uuid.UUID
    quiet_end_user_id: uuid.UUID


def metrics_seed(acme: Organization, globex: Organization) -> MetricsSeed:
    acme_gateway_id, other_gateway_id, globex_gateway_id = uuid7(), uuid7(), uuid7()
    acme_model_id, mini_model_id = uuid7(), uuid7()

    logs: list[RequestLog] = []
    # Twenty successes on one gateway: the latency distribution the percentile checks read.
    for index, latency in enumerate(range(5, 101, 5)):
        logs.append(
            make_log_row(
                acme,
                gateway_id=acme_gateway_id,
                latency_total_ms=latency,
                upstream_model_id=acme_model_id,
                model_name="acme-gpt",
                prompt_tokens=15,
                completion_tokens=7,
                session_id="sess-7" if index == 0 else None,
            )
        )

    # Three streamed requests on a second gateway, ten minutes apart, one per status
    # class — the fixture for bucketing, grouping and the error taxonomy at once.
    # The retrieval half rides on the same three rows: all three searched, and only the
    # first found anything. That makes the empty-retrieval rate on this gateway two in
    # three, against twenty requests on the other gateway that never searched at all —
    # which is exactly the distinction the rate has to survive.
    not_served = "Model 'gpt-9' is not served by gateway 'acme-chat'."
    timed_out = "[upstream:acme-gpt] did not respond within 5s."
    #: status, error code, message, ttft, total, retrieval ms, found anything
    streamed = (
        (200, None, None, 10, 50, 12, True),
        (404, "model_not_found", not_served, 20, 60, 30, False),
        (504, "upstream_timeout", timed_out, 30, 70, 45, False),
    )
    for index, (status, code, message, ttft, latency, retrieval_ms, found) in enumerate(streamed):
        logs.append(
            make_log_row(
                acme,
                gateway_id=other_gateway_id,
                created_at=NOW + timedelta(minutes=10 * index),
                status_code=status,
                error_code=code,
                error_message=message,
                streamed=True,
                latency_ttft_ms=ttft,
                latency_total_ms=latency,
                latency_retrieval_ms=retrieval_ms,
                memory_tokens=180 if found else None,
                retrieved_chunk_ids=(
                    [
                        {
                            "id": "chunk-1",
                            "score": 0.71,
                            "document_id": str(uuid7()),
                            "source_name": "handbook.md",
                            "page_or_section": "p. 12",
                            "chunk_index": 0,
                            "injected": True,
                        }
                    ]
                    if found
                    else []
                ),
                upstream_model_id=mini_model_id,
                model_name="acme-mini",
                completion_tokens=10 if status == 200 else None,
            )
        )

    globex_log = make_log_row(globex, gateway_id=globex_gateway_id, model_name="globex-gpt")
    logs.append(globex_log)

    # SPEC §11's throttling, in its own window. Three rejections for one caller, one for
    # another, one for nobody in particular, and one belonging to the other organization.
    noisy = make_end_user(acme, "noisy-bot")
    quiet = make_end_user(acme, "quiet-app")
    intruder = make_end_user(globex, "globex-bot")
    throttled: list[tuple[Organization, uuid.UUID, EndUser | None]] = [
        (acme, acme_gateway_id, noisy),
        (acme, acme_gateway_id, noisy),
        (acme, other_gateway_id, noisy),
        (acme, acme_gateway_id, quiet),
        # Nobody identified this one. It must not become an "anonymous" bar, because that
        # is not a caller anybody can go and talk to.
        (acme, acme_gateway_id, None),
        (globex, globex_gateway_id, intruder),
    ]
    for index, (owner, gateway_id, who) in enumerate(throttled):
        logs.append(
            make_log_row(
                owner,
                gateway_id=gateway_id,
                created_at=THROTTLED_AT + timedelta(seconds=index),
                status_code=429,
                error_code="rate_limited",
                error_message="Rate limit exceeded: 10 requests per minute for this gateway.",
                bodies_omitted="rate_limited",
                end_user_id=who.id if who is not None else None,
            )
        )

    return MetricsSeed(
        logs=tuple(logs),
        end_users=(noisy, quiet, intruder),
        noisy_end_user_id=noisy.id,
        quiet_end_user_id=quiet.id,
        # Only the first row gets bodies, so "no transcript" and "no row" stay
        # distinguishable — which is exactly what the detail drawer has to tell apart.
        transcripts=(
            Transcript(
                request_log_id=logs[0].id,
                created_at=logs[0].created_at,
                organization_id=acme.id,
                request_body=[{"role": "user", "content": "what is the answer"}],
                assembled_prompt=[
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "what is the answer"},
                ],
                response_body="the answer",
            ),
        ),
        acme_gateway_id=acme_gateway_id,
        other_gateway_id=other_gateway_id,
        globex_gateway_id=globex_gateway_id,
        acme_log_id=logs[0].id,
        bodiless_log_id=logs[1].id,
        globex_log_id=globex_log.id,
    )


def make_end_user(organization: Organization, external_id: str) -> EndUser:
    """An ``end_users`` row for a caller the log rows can point at."""
    return EndUser(
        id=uuid7(),
        organization_id=organization.id,
        external_id=external_id,
        first_seen_at=NOW,
        last_seen_at=NOW,
        request_count=1,
    )


__all__ = [
    "NOW",
    "THROTTLED_AT",
    "BrokenSummaryCache",
    "FakeSummaryCache",
    "LogFixture",
    "MetricsSeed",
    "build_logs",
    "build_monitoring",
    "id_at",
    "make_log_row",
    "metrics_seed",
    "seed",
    "submitted",
]
