"""Where a flushed batch of request logs lands.

The write half of request logging, kept apart from the read half in
:mod:`app.services.metrics_store` because the two have opposite shapes. Writes are
unscoped, batched, and cross-tenant by construction — one flush holds whatever traffic
the last half-second brought, from every organization on the process. Reads are scoped to
one organization and go through :class:`~app.db.scoping.ScopedRepository` like everything
else.

That asymmetry is why the insert declares :func:`~app.db.scoping.unscoped` with a reason
rather than being quietly exempt. It is the one place in the system that deliberately
writes rows for several tenants in a single statement, and it should be greppable as
such.

Two implementations of one shape, as everywhere else here. ``metadata_row`` and
``transcript_row`` are shared, so the two cannot disagree about what a record becomes;
the memory writer stores *mapped objects* built from those same dicts rather than the
dicts themselves, which is what makes a forgotten column show up as a wrong answer in
``tests/metrics_store_contract.py`` instead of a missing key nobody asserts on.
``tests/test_metrics_db.py`` runs the PostgreSQL writer for real, including the
cross-tenant batch.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RequestLog, Transcript
from app.db.scoping import unscoped
from app.services.memory_db import MemoryDatabase
from app.services.request_log import RequestRecord, as_json

logger = logging.getLogger(__name__)


def metadata_row(record: RequestRecord) -> dict[str, Any]:
    """The ``request_logs`` row for a record.

    A plain dict rather than an ORM instance: the batch goes through a single
    multi-row ``INSERT``, and constructing a hundred mapped objects only to have
    SQLAlchemy flatten them again is work the flusher does not need to do.
    """
    return {
        "id": record.id,
        "created_at": record.created_at,
        "organization_id": record.organization_id,
        "gateway_id": record.gateway_id,
        "api_key_id": record.api_key_id,
        "end_user_id": record.end_user_id,
        "session_id": record.session_id,
        "upstream_model_id": record.upstream_model_id,
        "model_name": record.model_name,
        "status_code": record.status_code,
        "error_code": record.error_code,
        "error_message": record.error_message,
        "streamed": record.streamed,
        "latency_total_ms": record.latency_total_ms,
        "latency_retrieval_ms": record.latency_retrieval_ms,
        "latency_ttft_ms": record.latency_ttft_ms,
        "latency_upstream_ms": record.latency_upstream_ms,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "memory_tokens": record.memory_tokens,
        "retrieved_chunk_ids": list(record.retrieved_chunk_ids),
        "retrieved_fact_ids": list(record.retrieved_fact_ids),
        "cited_chunk_ids": list(record.cited_chunk_ids),
        "citations_unresolved": record.citations_unresolved,
        "tokenizer": record.tokenizer,
        "estimated_prompt_tokens": record.estimated_prompt_tokens,
        "template_fingerprint": record.template_fingerprint,
        "failover_attempts": list(record.failover_attempts),
        "dropped_params": list(record.dropped_params),
        "request_id": record.request_id,
        "response_truncated": record.response_truncated,
        "failed_after_stream_start": record.failed_after_stream_start,
        "bodies_omitted": record.bodies_omitted,
    }


def transcript_row(record: RequestRecord) -> dict[str, Any] | None:
    """The ``transcripts`` row, or ``None`` when there is nothing to store.

    ``created_at`` is copied from the metadata row rather than defaulted, because it is
    the partition key on both tables: a transcript written a moment after midnight would
    otherwise land in tomorrow's partition while its metadata sits in yesterday's, and
    task 17's retention drop would take one without the other.
    """
    if not record.has_bodies:
        return None
    return {
        "request_log_id": record.id,
        "created_at": record.created_at,
        "organization_id": record.organization_id,
        "request_body": as_json(record.request_body),
        "assembled_prompt": as_json(record.assembled_prompt),
        "response_body": record.response_body,
        "distilled_at": None,
    }


class PostgresLogWriter:
    """One transaction per batch: metadata first, then the bodies that have them.

    Both statements share a transaction so a transcript can never outlive a metadata row
    that was rolled back — the detail view resolves a transcript through its log row, and
    an orphan would be invisible and undeletable.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def write(self, records: Sequence[RequestRecord]) -> None:
        if not records:
            return

        metadata = [metadata_row(record) for record in records]
        transcripts = [row for row in map(transcript_row, records) if row is not None]

        async with self._session_factory() as session:
            await session.execute(
                insert(RequestLog).execution_options(
                    **unscoped("the log flusher batches records from every organization")
                ),
                metadata,
            )
            if transcripts:
                await session.execute(
                    insert(Transcript).execution_options(
                        **unscoped("the log flusher batches records from every organization")
                    ),
                    transcripts,
                )
            await session.commit()


class MemoryLogWriter:
    """The same two rows, in dictionaries.

    Deliberately stores mapped objects rather than the dicts above, because the read side
    in :mod:`app.services.metrics_store` reads attributes — so a column this writer
    forgot to set shows up as a wrong answer in the contract test rather than as a
    missing key nobody asserts on.
    """

    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    async def write(self, records: Sequence[RequestRecord]) -> None:
        for record in records:
            self._db.request_logs[record.id] = RequestLog(**metadata_row(record))
            row = transcript_row(record)
            if row is not None:
                self._db.transcripts[record.id] = Transcript(**row)


__all__ = ["MemoryLogWriter", "PostgresLogWriter", "metadata_row", "transcript_row"]
