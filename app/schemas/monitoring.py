"""Response shapes for the monitoring screen.

Two things are load-bearing here, and both are about what the client is *not* asked to
work out for itself.

**A log row says what was not stored, and why.** ``bodies_omitted`` and the three
``*_captured`` flags exist so the detail drawer can render "not captured — response body
logging is off for this gateway" instead of an empty panel. An empty panel and a request
that genuinely had no response body look identical, and the second is a bug report.

**The transcript is a separate object that may be absent.** It is never partially filled
in: either the bodies are there or the reason they are not is. That mirrors the storage —
``transcripts`` is a row that exists or does not — so the API cannot imply a state the
database cannot hold.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from app.db.models import RequestLog, Transcript
from app.schemas.routing import AttemptResponse
from app.services.distillation_service import ManualPass
from app.services.distillation_store import MemoryHealth
from app.services.metrics_store import Bucket, LogDetail, Summary
from app.services.monitoring import Series

_CONFIG = ConfigDict(extra="forbid")


class PercentilesResponse(BaseModel):
    model_config = _CONFIG

    p50: int | None = None
    p95: int | None = None
    p99: int | None = None


class ModelTrafficResponse(BaseModel):
    model_config = _CONFIG

    upstream_model_id: uuid.UUID | None = None
    model_name: str | None = None
    requests: int


class ErrorGroupResponse(BaseModel):
    model_config = _CONFIG

    error_code: str
    requests: int


class SummaryResponse(BaseModel):
    """The cards, the latency numbers, and the two distribution charts."""

    model_config = _CONFIG

    requests: int
    errors: int
    #: Precomputed rather than left to the client: two screens and a dashboard card show
    #: it, and three roundings of the same division is three chances to disagree.
    error_rate: float
    status_classes: dict[str, int]
    total: PercentilesResponse
    ttft: PercentilesResponse
    retrieval: PercentilesResponse
    prompt_tokens: int
    completion_tokens: int
    memory_tokens: int
    #: Requests where retrieval ran, and how many of those injected nothing. Precomputed
    #: as a rate for the same reason ``error_rate`` is: three screens divide it, and three
    #: roundings of one division is three chances to disagree.
    retrieval_attempts: int
    retrieval_empty: int
    empty_retrieval_rate: float
    models: list[ModelTrafficResponse]
    error_groups: list[ErrorGroupResponse]

    @classmethod
    def of(cls, summary: Summary) -> Self:
        return cls(
            requests=summary.requests,
            errors=summary.errors,
            error_rate=round(summary.error_rate, 6),
            status_classes=dict(summary.status_classes),
            total=PercentilesResponse(**asdict(summary.total)),
            ttft=PercentilesResponse(**asdict(summary.ttft)),
            retrieval=PercentilesResponse(**asdict(summary.retrieval)),
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            memory_tokens=summary.memory_tokens,
            retrieval_attempts=summary.retrieval_attempts,
            retrieval_empty=summary.retrieval_empty,
            empty_retrieval_rate=round(summary.empty_retrieval_rate, 6),
            models=[ModelTrafficResponse(**asdict(model)) for model in summary.models],
            error_groups=[ErrorGroupResponse(**asdict(group)) for group in summary.error_groups],
        )


class BucketResponse(BaseModel):
    model_config = _CONFIG

    start: datetime
    #: Series name to value. Names are ``requests``; ``prompt``/``completion``/``memory``;
    #: ``total_p50``/``total_p95``/``total_p99``/``ttft_p95``/``retrieval_p95``; and, for
    #: the ``retrieval`` metric, ``attempts``/``empty``/``empty_rate``/``p95``. Prefixed
    #: with the group when one was asked for, as in ``5xx.requests``.
    series: dict[str, float]

    @classmethod
    def of(cls, bucket: Bucket) -> Self:
        return cls(start=bucket.start, series=dict(bucket.series))


class SeriesResponse(BaseModel):
    model_config = _CONFIG

    #: What the server actually used, which may be coarser than what was asked for.
    interval_seconds: int
    buckets: list[BucketResponse]

    @classmethod
    def of(cls, series: Series) -> Self:
        return cls(
            interval_seconds=series.interval_seconds,
            buckets=[BucketResponse.of(bucket) for bucket in series.buckets],
        )


class RequestLogResponse(BaseModel):
    """One row of the request table. No bodies — that is what the detail view is for."""

    model_config = _CONFIG

    id: uuid.UUID
    created_at: datetime
    gateway_id: uuid.UUID
    api_key_id: uuid.UUID | None
    end_user_id: uuid.UUID | None
    session_id: str | None
    upstream_model_id: uuid.UUID | None
    model_name: str | None
    status_code: int
    error_code: str | None
    error_message: str | None
    streamed: bool
    latency_total_ms: int
    latency_retrieval_ms: int | None
    latency_ttft_ms: int | None
    latency_upstream_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    memory_tokens: int | None
    request_id: str | None
    response_truncated: bool
    #: SPEC §8.2: the upstream died after the first chunk was flushed, so failover could
    #: not have helped. Distinct from ``error_code``, which says *what* went wrong.
    failed_after_stream_start: bool
    bodies_omitted: str | None

    @classmethod
    def of(cls, row: RequestLog) -> Self:
        return cls(
            id=row.id,
            created_at=row.created_at,
            gateway_id=row.gateway_id,
            api_key_id=row.api_key_id,
            end_user_id=row.end_user_id,
            session_id=row.session_id,
            upstream_model_id=row.upstream_model_id,
            model_name=row.model_name,
            status_code=row.status_code,
            error_code=row.error_code,
            error_message=row.error_message,
            streamed=bool(row.streamed),
            latency_total_ms=row.latency_total_ms,
            latency_retrieval_ms=row.latency_retrieval_ms,
            latency_ttft_ms=row.latency_ttft_ms,
            latency_upstream_ms=row.latency_upstream_ms,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            memory_tokens=row.memory_tokens,
            request_id=row.request_id,
            response_truncated=row.response_truncated,
            failed_after_stream_start=bool(row.failed_after_stream_start),
            bodies_omitted=row.bodies_omitted,
        )


class TranscriptResponse(BaseModel):
    """The bodies, as far as they were stored."""

    model_config = _CONFIG

    request_body: list[dict[str, Any]] | None = None
    assembled_prompt: list[dict[str, Any]] | None = None
    response_body: str | None = None
    distilled_at: datetime | None = None

    @classmethod
    def of(cls, row: Transcript) -> Self:
        return cls(
            request_body=row.request_body,
            assembled_prompt=row.assembled_prompt,
            response_body=row.response_body,
            distilled_at=row.distilled_at,
        )


class RequestDetailResponse(BaseModel):
    """One request, with everything the §10.3 drawer draws.

    The retrieval lists are here and empty until task 10 fills them, for the same reason
    the gateway editor shows its unbuilt sections: a drawer that grows a panel later moves
    everything the reader has learned the position of.

    ``failover_attempts`` is empty for the overwhelming majority of requests, and that is
    information rather than an omission: it means one target answered, which the ``log``
    fields already describe completely. Non-empty means more than one was involved.
    """

    model_config = _CONFIG

    log: RequestLogResponse
    transcript: TranscriptResponse | None = None
    retrieved_chunk_ids: list[Any]
    retrieved_fact_ids: list[Any]
    failover_attempts: list[AttemptResponse]

    @classmethod
    def of(cls, detail: LogDetail) -> Self:
        return cls(
            log=RequestLogResponse.of(detail.log),
            transcript=(
                TranscriptResponse.of(detail.transcript) if detail.transcript is not None else None
            ),
            retrieved_chunk_ids=list(detail.log.retrieved_chunk_ids or []),
            retrieved_fact_ids=list(detail.log.retrieved_fact_ids or []),
            failover_attempts=[
                AttemptResponse.model_validate(attempt)
                for attempt in (detail.log.failover_attempts or [])
            ],
        )


class MemoryHealthDayResponse(BaseModel):
    """One day of the memory-health chart."""

    model_config = _CONFIG

    day: datetime
    runs: int = 0
    failures: int = 0
    written: int = 0
    deduped: int = 0
    superseded: int = 0


class MemoryHealthResponse(BaseModel):
    """SPEC §10.1's memory health, with the rates computed here rather than in the browser.

    The three ratios are sent rather than left to the client, because each has a
    denominator that is easy to get subtly wrong — a failure rate over *successful* passes,
    a dedupe rate over inserted facts instead of proposed ones — and a chart that quietly
    disagrees with the alert is worse than no chart.
    """

    model_config = _CONFIG

    days: list[MemoryHealthDayResponse]
    runs: int = 0
    failures: int = 0
    written: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    rejected: int = 0
    candidates: int = 0
    facts: int = 0
    end_users_with_facts: int = 0
    failure_rate: float = 0.0
    dedupe_rate: float = 0.0
    supersession_rate: float = 0.0
    average_facts_per_end_user: float = 0.0

    @classmethod
    def of(cls, health: MemoryHealth) -> Self:
        return cls(
            days=[MemoryHealthDayResponse(**asdict(day)) for day in health.days],
            runs=health.runs,
            failures=health.failures,
            written=health.written,
            deduped=health.deduped,
            superseded=health.superseded,
            evicted=health.evicted,
            rejected=health.rejected,
            candidates=health.candidates,
            facts=health.facts,
            end_users_with_facts=health.end_users_with_facts,
            failure_rate=health.failure_rate,
            dedupe_rate=health.dedupe_rate,
            supersession_rate=health.supersession_rate,
            average_facts_per_end_user=health.average_facts_per_end_user,
        )


class ManualPassResponse(BaseModel):
    """What "Distil now" did.

    ``reason`` is the field that makes the button useful. Zero facts written has several
    causes — nothing new was said, the daily cap is spent, no model is configured, the
    extractor found nothing durable — and they need four different actions.
    """

    model_config = _CONFIG

    sessions: int = 0
    inserted: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    rejected: int = 0
    reason: str | None = None

    @classmethod
    def of(cls, result: ManualPass) -> Self:
        return cls(**asdict(result))


__all__ = [
    "AttemptResponse",
    "BucketResponse",
    "ErrorGroupResponse",
    "ManualPassResponse",
    "MemoryHealthDayResponse",
    "MemoryHealthResponse",
    "ModelTrafficResponse",
    "PercentilesResponse",
    "RequestDetailResponse",
    "RequestLogResponse",
    "SeriesResponse",
    "SummaryResponse",
    "TranscriptResponse",
]
