"""The validation tables (task 103, SPEC §6.6): audit reports, evaluation sets, and runs.

Four tables for three screens, and each one exists because the thing it holds has to
outlive a request.

**``index_audits``** is one row per audit of one connector's index — chunking or embedding
— with the whole report as JSONB. A report is minutes of scrolling a collection, and the
screen reads the latest one and says how old it is; recomputing it on every page load
would be the cost the job exists to avoid. History is kept so that "the short-chunk count
went from 312 to 4 after the recut" is a query rather than a memory.

**``evaluation_sets``** and **``evaluation_items``** are an organization's labelled
questions. The set belongs to a gateway because the labels only mean something against the
connectors that gateway reads; a question about the handbook is not a wrong answer for the
support gateway, it is a question about a corpus it cannot see. An item's ``relevant``
labels carry the **chunk's text at labelling time**, which is what lets a label survive a
reindex: the chunk id dies with the recut, the text does not, and the run re-anchors by it.

**``evaluation_runs``** is a measurement of a known state. The row stores the effective
configuration it ran with, the connectors' chunk fingerprints and the embedding model at the
time, and the per-item results — so two runs can be diffed and the diff can say *what
changed between them* rather than only that the number moved. Results are JSONB on the row
rather than a fifth table because a run is capped at ``MAX_ITEMS_PER_RUN`` items and is read
whole or not at all.

No foreign key from an item's label to a document or a chunk: the label is a record of what
somebody said was relevant, and it stays true after the document is deleted — the run
reports it as unanchored, which is information.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

AUDIT_KINDS = ("chunking", "embedding")
AUDIT_STATUSES = ("running", "succeeded", "failed")

#: Where a label came from. A number computed over labels a model wrote is a different
#: number from one over labels a person checked, and the report says which.
ITEM_SOURCES = ("log", "citation", "manual", "generated")

RUN_STATUSES = ("queued", "running", "succeeded", "failed")

MAX_SET_NAME_LENGTH = 200
MAX_QUESTION_LENGTH = 4000


class IndexAudit(Base, UUIDPrimaryKeyMixin):
    """One audit of one connector's live collection."""

    __tablename__ = "index_audits"
    __table_args__ = (
        CheckConstraint("kind IN ('chunking', 'embedding')", name="kind_is_known"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status_is_known"),
        # The screen asks for the latest per (connector, kind); the dashboard for the
        # latest per connector across the organization.
        Index("ix_index_audits_connector_id_kind_created_at", "connector_id", "kind", "created_at"),
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
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: The embedding audit's one spending option: how many chunks to re-embed. Null for a
    #: chunking audit, and for an embedding audit that was asked not to.
    drift_sample: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The report, as :func:`app.services.index_audit.report_json` writes it. Empty while
    #: running and after a failure.
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: The worst finding's severity, denormalised so the dashboard's degraded list is one
    #: indexed read rather than a JSONB scan.
    severity: Mapped[str | None] = mapped_column(String(8), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationSet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "evaluation_sets"
    __table_args__ = (Index("ix_evaluation_sets_gateway_id", "gateway_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gateway_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("gateways.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(MAX_SET_NAME_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class EvaluationItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One labelled question. Negative when it has no label at all."""

    __tablename__ = "evaluation_items"
    __table_args__ = (
        CheckConstraint(
            "source IN ('log', 'citation', 'manual', 'generated')", name="source_is_known"
        ),
        Index("ix_evaluation_items_set_id", "set_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    set_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evaluation_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: Chunk-level labels: ``[{chunk_id, document_id, source_name, text}]``. ``text`` is
    #: the chunk's text when it was labelled, for re-anchoring after a recut.
    relevant: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: Document-level labels, as ids. A person can usually say which document answers a
    #: question and rarely which chunk.
    relevant_document_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    #: Whether a person has confirmed the labels. Imported and generated items arrive
    #: unverified; a run reports the two populations separately.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvaluationRun(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status_is_known"
        ),
        Index("ix_evaluation_runs_set_id_created_at", "set_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    set_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evaluation_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    gateway_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: The unsaved patch the run was asked to apply, if any — so the history can say
    #: "this row is the form, not the saved gateway".
    patch: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: The effective memory configuration the retrieval ran with, patch merged.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: The state of the index at the time: embedding model, tokenizer, and per connector
    #: the chunk fingerprints its documents were cut with. What a diff names.
    snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    results: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = [
    "AUDIT_KINDS",
    "AUDIT_STATUSES",
    "ITEM_SOURCES",
    "MAX_QUESTION_LENGTH",
    "MAX_SET_NAME_LENGTH",
    "RUN_STATUSES",
    "EvaluationItem",
    "EvaluationRun",
    "EvaluationSet",
    "IndexAudit",
]
