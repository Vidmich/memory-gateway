"""Reprocessing runs (task 104): recutting a connector's documents as a tracked operation.

One row per run. Before this table a connector-wide reindex was a burst of anonymous
ingestion jobs: the button returned a count, the queue drained, and the only record of
the operation was a log line. The row is what turns "is it still going" into a progress
bar and "what did that cost" into a column — and what lets a run that a dead worker left
half done be *continued* rather than started over, because the counters survive and the
documents it still owns are marked with its id.

The units are documents, not points, which is why this is a table of its own rather than a
row in task 17's ``reindex_runs``: that run rebuilds a *collection* and counts points across
every tenant; this one re-ingests one connector's files inside one organization. The platform
reindex creates one of these per connector it recuts, so the connector's screen shows the
platform operation's progress for its own documents without knowing about the platform
screen — ``reindex_run_id`` is the link.

``estimated_tokens`` beside ``spent_tokens`` is the calibration for the next estimate and
the audit trail for the bill.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

#: What made the documents stale. ``manual`` is a run started over documents that were
#: not — a whole-connector reprocess, or one over unrecorded rows.
REPROCESSING_TRIGGERS = (
    "chunking",
    "embedding_model",
    "tokenizer",
    "summarization",
    "extractor",
    "manual",
)

#: Which documents the run was started over.
REPROCESSING_SCOPES = ("stale", "formats", "all", "unrecorded", "failed")

#: ``running`` until every document it enqueued has finished; then one of the three
#: outcomes. ``partial`` is a run that finished with failures — the documents that failed
#: keep their reason, and **Retry failed** starts a new run over exactly those.
REPROCESSING_STATUSES = ("running", "succeeded", "partial", "failed")

#: Documents' second status axis (SPEC §9.5, amended by task 104). ``current`` means the
#: row's index fingerprint is the one ingestion would write now; ``stale`` that it is
#: not; ``reprocessing`` that a run owns it. Orthogonal to ingestion status: a document is
#: ``indexed`` and ``stale`` at once, and that is the normal state after a change.
INDEX_STATUSES = ("current", "stale", "reprocessing")


class ReprocessingRun(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "reprocessing_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger IN ('chunking', 'embedding_model', 'tokenizer', 'summarization',"
            " 'extractor', 'manual')",
            name="trigger_is_known",
        ),
        CheckConstraint(
            "scope IN ('stale', 'formats', 'all', 'unrecorded', 'failed')",
            name="scope_is_known",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="status_is_known",
        ),
        CheckConstraint(
            "total >= 0 AND done >= 0 AND failed >= 0 AND skipped >= 0",
            name="counters_are_not_negative",
        ),
        # The history drawer reads a connector's runs newest first; the progress bar
        # reads the one still running.
        Index("ix_reprocessing_runs_connector_id_started_at", "connector_id", "started_at"),
        Index("ix_reprocessing_runs_reindex_run_id", "reindex_run_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The format kinds the run covers, or empty for every format.
    formats: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: Who pressed the button, or null for a run the platform reindex spawned. The label
    #: is the actor's email at the time, denormalised for the same reason the audit trail
    #: does it: the history has to stay readable after the account is gone.
    requested_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    requested_by_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The platform reindex that spawned this run, when one did. No foreign key: that
    #: table is platform-owned and lives under a different retention.
    reindex_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Embedding tokens the run was expected to spend, and what it actually spent — the
    #: sum of every chunk's token count as each document lands.
    estimated_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    spent_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Bumped each time the reconciliation job continued the run after a worker died, so
    #: the re-enqueued jobs carry a key the queue has not seen.
    resumed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: What this run cost or found, beyond the counters: reasons for the skipped
    #: documents, and anything the platform reindex wants to say about its share.
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def settled(self) -> int:
        return self.done + self.failed + self.skipped

    @property
    def running(self) -> bool:
        return self.finished_at is None


def settle(run: ReprocessingRun, outcome: str, *, tokens: int, now: datetime | None = None) -> None:
    """One more document of the run reached ``done``, ``failed`` or ``skipped``.

    The one place the counters move and the one place the run closes, used by both
    stores under whatever atomicity each has (a row lock in PostgreSQL, the single thread
    in memory). The run finishes when every document has settled: ``succeeded`` with no
    failures, ``partial`` with some, ``failed`` when nothing at all came through.
    """
    if outcome == "done":
        run.done += 1
    elif outcome == "failed":
        run.failed += 1
    elif outcome == "skipped":
        run.skipped += 1
    else:
        raise ValueError(f"unknown reprocessing outcome {outcome!r}")
    run.spent_tokens += max(0, int(tokens))
    if run.finished_at is None and run.settled >= run.total:
        run.finished_at = now or datetime.now(UTC)
        if run.failed == 0:
            run.status = "succeeded"
        elif run.done + run.skipped == 0:
            run.status = "failed"
        else:
            run.status = "partial"


__all__ = [
    "INDEX_STATUSES",
    "REPROCESSING_SCOPES",
    "REPROCESSING_STATUSES",
    "REPROCESSING_TRIGGERS",
    "ReprocessingRun",
    "settle",
]
