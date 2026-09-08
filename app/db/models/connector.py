"""Connectors and the documents they ingest.

A connector is a *source* of content plus the settings for turning it into vectors. The
v1 type is ``managed_file_drop`` — a prefix in the platform's object store that customers
upload into — but nothing in this table is about S3: ``type`` selects an implementation
of the port in :mod:`app.services.connector_source`, ``config`` carries whatever that
implementation needs, and ``storage_prefix`` is null for a type that holds no bytes of
ours. SPEC §16.3's SQL and HTTP connectors are then rows with a different ``type``.

``documents`` is both the unit of work and the unit of reporting. One row per object,
carrying what it is (``source_uri``, ``mime_type``, ``size_bytes``) and where the pipeline
got to (``status``, ``error``, ``chunk_count``, ``indexed_at``). SPEC §9.5's progression
lives in that one column precisely so the UI, the retry action, and resync reconciliation
read the same fact rather than three that can disagree.

Two columns exist for change detection and they are not redundant. ``etag`` is the store's
answer to "is this the same object", and it costs nothing to read during a listing;
``content_hash`` is ours, a SHA-256 over the bytes actually ingested. An ETag is not a
content hash for a multipart upload and its derivation differs between storage backends,
so a resync compares ETags to decide what to *look at* and hashes to decide what to *redo*.
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: SPEC §9.1. One type in v1; a string with a CHECK, so adding the next one is a
#: constraint swap rather than an ``ALTER TYPE`` that cannot run inside a transaction.
CONNECTOR_TYPES = ("managed_file_drop",)

#: ``syncing`` is set by a resync and cleared when it finishes. ``deleting`` is set
#: *before* the delete job is enqueued, so a connector whose objects are being removed
#: cannot be uploaded into and reads as going away while the bytes are still going.
CONNECTOR_STATUSES = ("ready", "syncing", "deleting", "error")

#: SPEC §9.5, in order. The first five are the happy path; the last two are terminal.
DOCUMENT_STATUSES = (
    "pending",
    "extracting",
    "chunking",
    "embedding",
    "indexed",
    "failed",
    "skipped",
)

#: Statuses from which nothing happens on its own. Everything else means a job is running
#: or is about to; a resync leaves those alone rather than racing them.
TERMINAL_DOCUMENT_STATUSES = ("indexed", "failed", "skipped")


class Connector(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "connectors"
    __table_args__ = (
        CheckConstraint("type IN ('managed_file_drop')", name="type_is_known"),
        CheckConstraint(
            "status IN ('ready', 'syncing', 'deleting', 'error')", name="status_is_known"
        ),
        UniqueConstraint("organization_id", "name", name="uq_connectors_organization_id_name"),
        Index("ix_connectors_organization_id_id", "organization_id", "id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="managed_file_drop")

    #: Type-specific settings. Empty for a managed file drop, whose only configuration is
    #: the prefix below; an S3-external or HTTP connector puts its endpoint here.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: SPEC §9.3, validated by :class:`app.schemas.connector_config.ChunkingConfig`.
    chunking: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    #: ``orgs/{org_id}/connectors/{connector_id}/``. Derived, never supplied: a customer
    #: who could choose their own prefix could choose another tenant's.
    storage_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", server_default="ready"
    )
    #: Why the connector itself is unhealthy — a listing that failed, a delete that could
    #: not finish. Per-document failures live on the document, not here.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    documents: Mapped[list[Document]] = relationship(
        back_populates="connector",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Document(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'extracting', 'chunking', 'embedding', 'indexed',"
            " 'failed', 'skipped')",
            name="status_is_known",
        ),
        CheckConstraint("size_bytes >= 0", name="size_is_not_negative"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_is_not_negative"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="page_count_is_not_negative"),
        # The reconciliation key. Two ingestions of the same object racing each other must
        # produce one row, and this is what makes that a database guarantee rather than a
        # hopeful SELECT followed by an INSERT.
        UniqueConstraint("connector_id", "source_uri", name="uq_documents_connector_id_source_uri"),
        # The document table is always read for one connector, newest first.
        Index("ix_documents_connector_id_id", "connector_id", "id"),
        Index("ix_documents_organization_id_id", "organization_id", "id"),
        # Backs the per-status counts on the connectors list, which would otherwise be a
        # sequential scan per row on the page.
        Index("ix_documents_connector_id_status", "connector_id", "status"),
    )

    #: Denormalised from the connector so every read is scoped by the same column as every
    #: other tenant-keyed table — :class:`~app.db.scoping.ScopedRepository` and the scope
    #: guard both work off ``organization_id``, and a join-derived tenant is one refactor
    #: away from being lost.
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

    #: The object's full storage key. Stable across re-uploads of the same name, which is
    #: what makes a changed file an update rather than a second row.
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    #: What a person calls it: the last path segment. Shown in the UI and carried into
    #: every chunk's payload, because a retrieved chunk has to be attributable.
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Sniffed from the first bytes, not taken from the extension or from the client.
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    #: SHA-256 of the raw bytes. Null until something has read them.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The object store's version marker, as returned by a listing.
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    #: A sentence a customer can act on: which byte failed to decode, which extension is
    #: not supported yet. Never a traceback.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The same fact as a stable code — ``needs_ocr``, ``password_protected``,
    #: ``unsupported_format``. The sentence above is for a person and gets rewritten; this
    #: is what the UI branches on to turn a failure into an explained state with a way out
    #: of it. Not CHECK-constrained, unlike ``status``: reasons are open by design, so a
    #: new extractor can explain a new failure without a migration, and a code the UI does
    #: not recognise falls back to showing the sentence.
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Pages, slides or sheets — whichever unit the format has, with the noun derived in
    #: the UI from the media type. ``NULL`` where the format has none: a Word document's
    #: pagination is a rendering decision, so a number here would be invented.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: SPEC §9.4 — recorded per document so a platform embedding-model change is
    #: detectable as drift instead of silently degrading retrieval.
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    connector: Mapped[Connector] = relationship(back_populates="documents")


class JobDeadLetter(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A job that exhausted its retries.

    Deliberately **not** tenant-keyed. The customer-visible half of an ingestion failure
    is already on ``documents.error``, next to the retry button; this is the operator's
    half — the payload, the attempt count, and the request id needed to reproduce it.
    Giving it an ``organization_id`` would make it a tenant table that the worker has to
    invent a scope for on a path with no request and no session, and the surest way not
    to get that wrong is to have no column to get wrong. The originating organization is
    in the payload, for anyone reading a specific record.
    """

    __tablename__ = "job_dead_letters"
    __table_args__ = (
        CheckConstraint("attempts > 0", name="attempts_is_positive"),
        Index("ix_job_dead_letters_job_name_id", "job_name", "id"),
    )

    job_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The key the queue deduplicates on, so a dead letter can be matched to the enqueue
    #: that produced it.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    #: The control-plane request that enqueued the job, carried through every retry, so a
    #: worker log line joins to the API log line that caused it.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
