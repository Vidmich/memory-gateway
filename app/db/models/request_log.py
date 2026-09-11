"""Request logs and transcripts — the record of what actually happened.

Two tables, split on purpose. ``request_logs`` is the narrow metadata row every
monitoring query reads: ids, status, latencies, token counts. ``transcripts`` holds the
large text — the client's messages, the assembled prompt, the completion — and is read
only when somebody opens one request. Keeping them together would mean the p95 latency
chart scans megabytes of prompt text to compute a number that lives in four bytes.

**Both are declaratively partitioned by day on ``created_at``.** SQLAlchemy has no
partitioning support, so these classes describe the *parent* table and the migration
emits the ``PARTITION BY RANGE`` DDL by hand. The reason for the partitioning is
retention: SPEC §10.2 gives every gateway a body-retention window and a longer metadata
window, and :mod:`app.services.maintenance` enforces those with a ``DROP TABLE`` on an
expired partition rather than a ``DELETE`` that rewrites a live table while the proxy is
writing to it. Windows differ per gateway and partitions do not, so a still-live day is
pruned per gateway by predicate — see that module for the two stages.

Two consequences of partitioning are visible in these models.

**The primary key carries ``created_at``.** PostgreSQL requires the partition key in
every unique constraint, so the key is ``(id, created_at)`` rather than ``id``. Nothing
outside this module cares — ids are UUIDv7, so an id already implies its day — but a
lookup by id alone scans every partition, which is why
:class:`~app.db.repositories.RequestLogRepository` takes the timestamp along.

**There are no foreign keys.** Not an oversight, and not laziness about partitioned-table
FK support. A log is a historical record: it has to survive the deletion of the gateway,
key or model it describes, because "what was this endpoint doing before I deleted it" is
a question people ask *after* deleting it. An ``ON DELETE CASCADE`` would answer it by
destroying the evidence, and an ``ON DELETE RESTRICT`` would make deleting a gateway fail
for as long as its traffic is retained. The detail view resolves names by lookup and says
"(deleted)" when one is gone; ``model_name`` is denormalised onto the row for the same
reason, so a chart can still label traffic that went to a model nobody kept.

``organization_id`` on ``transcripts`` is denormalised too, and that one is about
isolation rather than history: it is what puts the table inside
:func:`app.db.scoping.is_tenant_keyed`, so the scope guard covers a transcript read the
same way it covers everything else. The alternative — joining ``request_logs`` for every
body read — is the arrangement that made :class:`~app.db.repositories.ApiKeyRepository`
the one table the guard cannot see, and once was enough.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base

#: How many daily partitions exist ahead of today. The task 07 migration creates them
#: once and :class:`~app.services.maintenance.PartitionManager` keeps the window rolling;
#: this constant is what the two agree on, and a test asserts the migration's copy matches.
#: The ``_default`` partition the migration also leaves in place is the net under both.
PARTITION_DAYS_AHEAD = 30


class RequestLog(Base):
    """One completed request through the data plane (SPEC §14).

    Written from the background flusher, never from the request path. Nothing here is
    nullable that could be known: a null ``latency_ttft_ms`` means "not a stream", and a
    null ``prompt_tokens`` means the provider did not report usage — both are different
    from zero and the charts treat them so.
    """

    __tablename__ = "request_logs"
    __table_args__ = (
        Index("ix_request_logs_gateway_id_created_at", "gateway_id", "created_at"),
        Index("ix_request_logs_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_request_logs_end_user_id_created_at", "end_user_id", "created_at"),
        # Partial, because the error views are the only ones that filter on status and
        # 99% of rows are 2xx. An index over every row would be twenty times the size
        # for the same answer.
        Index(
            "ix_request_logs_errors",
            "organization_id",
            "created_at",
            postgresql_where=text("status_code >= 400"),
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)
    #: Part of the primary key because PostgreSQL requires the partition key in it. Also
    #: the value that routes the insert to a partition, so it is set by the collector at
    #: request time rather than defaulted by the server — a row must land in the day it
    #: happened, not the day it was flushed.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    gateway_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: Tasks 12 and 13 populate these. Indexed now because the index is part of the
    #: partition template, and adding one to a populated partitioned table later is a
    #: lock per partition.
    end_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    upstream_model_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: Denormalised so a chart can label traffic to a model that has since been deleted.
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: The gateway's own error code (``upstream_timeout``, ``rate_limited``), which is
    #: what the error taxonomy in SPEC §10.1 groups by. Null on success.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    streamed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    latency_total_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Task 10. Null until retrieval exists — which is not the same as zero, and a chart
    #: that plotted zero would claim the gateway adds no latency it has never measured.
    latency_retrieval_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Streamed requests only. A non-streamed response has no first token, and putting
    #: the total here would drag the TTFT percentiles toward the full generation time.
    latency_ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The provider call alone. With the two above it makes the §10.3 waterfall
    #: subtractable: total minus upstream minus retrieval is the gateway's own overhead.
    latency_upstream_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Tokens injected by the memory subsystem (task 10), counted separately so an
    #: organization can see what retrieval is costing them.
    memory_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    retrieved_chunk_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    retrieved_fact_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: Task 100. The ids of the injected chunks the answer cited, in order of first
    #: citation. ``retrieved_chunk_ids`` is what went into the prompt; this is what came
    #: back out of the answer; a chunk that is in the first on every request and never in
    #: the second is a retrieval false positive, and that ratio is what task 103 reads.
    #: Written for every request with documents injected, whatever the gateway's
    #: citation mode — ``off`` switches off the client's copy, not the record.
    cited_chunk_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: Handles the answer wrote that named no injected chunk. A count, because the
    #: number is the signal and the handles themselves are in the stored response.
    citations_unresolved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Task 101. Our own count of the prompt that went upstream, and the tokenizer that
    #: made it. Beside ``prompt_tokens`` — the provider's count — these are the
    #: calibration: the ratio of the two, summed per model over a window, is how far the
    #: tokenizer we measure budgets with is from the one the provider bills with. Null
    #: when nothing was assembled (a refused request) or, for the estimate, when the
    #: upstream never reported usage (a stream the client did not ask usage for).
    tokenizer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    estimated_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: One entry per attempted target — ``{target_id, model_name, status, error_code,
    #: latency_ms, retryable}`` — written only when more than one target was involved.
    #: Empty is the common case and means the row's own ``upstream_model_id`` and
    #: ``status_code`` already tell the whole story; see
    #: :meth:`app.services.routing.Attempts.as_json`.
    failover_attempts: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: SPEC §8.3. Generation parameters the target's dialect could not express, so they
    #: never reached the provider — ``presence_penalty`` against an Anthropic upstream, for
    #: instance. Empty for every OpenAI-shaped upstream, which is the overwhelming majority
    #: of rows, and the reason it is recorded at all is that a dropped parameter is
    #: otherwise invisible: the request succeeds and quietly ignores what was asked.
    dropped_params: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    #: The ``X-Gateway-Request-Id`` the client was given, so a support ticket quoting one
    #: leads to this row and to the matching structured log lines.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The streamed response outgrew the tee buffer, so the stored body is a prefix.
    response_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Why the bodies are missing when the gateway asked for them: ``queue_pressure`` or
    #: ``redaction_budget``. Null means nothing dropped them, so an absent transcript is
    #: the logging toggles doing their job. A reason rather than a boolean because the
    #: two causes need different actions — one is capacity, the other is somebody's
    #: regular expression — and the detail view has to be able to say which.
    bodies_omitted: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: SPEC §8.2. The upstream failed after the first chunk had been flushed, so failover
    #: was no longer possible and the client got a truncated answer under a 200. A column
    #: rather than an error code because ``status_code`` is honestly 200 and
    #: ``error_code`` is honestly ``stream_failed``: this is the third fact, the one that
    #: says why nothing recovered it.
    failed_after_stream_start: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class Transcript(Base):
    """The bodies for one request, subject to that gateway's logging configuration.

    A field is null when the corresponding toggle was off *or* when redaction could not
    complete — the two are distinguished by ``request_logs.bodies_dropped`` and by the
    gateway's stored configuration, which the detail view reads to say which it was.
    """

    __tablename__ = "transcripts"
    __table_args__ = (
        Index("ix_transcripts_organization_id_created_at", "organization_id", "created_at"),
        # Task 13 walks this to find transcripts it has not distilled yet.
        Index(
            "ix_transcripts_pending_distillation",
            "created_at",
            postgresql_where=text("distilled_at IS NULL"),
        ),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    request_log_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    #: The client's messages, exactly as sent — before any layer was prepended.
    request_body: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    #: What went upstream. The detail view diffs it against ``request_body`` so injected
    #: content is visually distinct from what the caller wrote.
    assembled_prompt: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    #: The completion text. Streamed responses are reassembled from their deltas, so this
    #: is identical to what the non-streamed call would have stored.
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Set by task 13 once conversation memory has been written from this transcript.
    distilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["PARTITION_DAYS_AHEAD", "RequestLog", "Transcript"]
