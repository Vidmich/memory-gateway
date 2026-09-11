"""One row per summarization attempt (task 102, SPEC §10.1 as amended).

The ledger. :mod:`app.db.models.distillation` explains why a background pass gets a table
and not just a counter — rates over a window outlive a process, and the daily cap has to be
counted from the same rows the chart reads — and every reason there holds here. One
argument is new, and it is the reason this table exists at all:

**The tokens are the point.** ``distillation_runs`` records what a pass *did*; it never
recorded what it *cost*, and the roadmap's usage-and-cost item (SPEC §16.2) has nothing to
build on. This table records ``tokens_in`` and ``tokens_out`` from day one, and records
them as **the provider's reported usage**, because a bill is what they are. When a provider
reports none, the estimate is stored and ``estimated`` says so — a number with a stated
error rather than a blank.

No foreign keys to ``upstream_models`` or ``documents``: the document a summary was written
for may have been deleted since, and the run is still a true record of what was spent.
``model_name`` is denormalised for the same reason ``request_logs`` denormalises it. The
``connector_id`` and ``document_id`` columns are plain ids for the same reason, indexed
because the cap is "this connector, today" and the panel's top-connectors list is "this
window, grouped by connector".

Pruned with the organization, like ``distillation_runs``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

#: What an attempt ended as. ``skipped`` is a cap hit or a missing model — the two refusals
#: an operator would otherwise report as "summaries have stopped appearing".
SUMMARIZATION_OUTCOMES = ("succeeded", "failed", "skipped")

MAX_REASON_LENGTH = 500

#: What the model call was for. ``summary`` is the phase this table was built for;
#: ``evaluation`` is task 103 writing a question from a chunk — the same chain, the same
#: bill, a different purpose, and the panel's document counts must not include it.
SUMMARIZATION_PURPOSES = ("summary", "evaluation")


class SummarizationRun(Base, UUIDPrimaryKeyMixin):
    """One attempt to summarize one document."""

    __tablename__ = "summarization_runs"
    __table_args__ = (
        CheckConstraint("outcome IN ('succeeded', 'failed', 'skipped')", name="outcome_is_known"),
        CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="tokens_are_not_negative"),
        CheckConstraint("purpose IN ('summary', 'evaluation')", name="purpose_is_known"),
        Index("ix_summarization_runs_organization_id_created_at", "organization_id", "created_at"),
        # The daily cap: this connector, since midnight.
        Index("ix_summarization_runs_connector_id_created_at", "connector_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, default="summary", server_default="summary"
    )
    reason: Mapped[str | None] = mapped_column(String(MAX_REASON_LENGTH), nullable=True)

    model_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: True when the provider reported no usage and the two counts above are ours.
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "MAX_REASON_LENGTH",
    "SUMMARIZATION_OUTCOMES",
    "SUMMARIZATION_PURPOSES",
    "SummarizationRun",
]
