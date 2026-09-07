"""Connectors, documents, and the job dead-letter table.

Three tables and one deliberate asymmetry.

``connectors`` and ``documents`` are tenant-keyed and cascade from ``organizations``:
deleting an organization takes its content with it, which is what SPEC §5.3 requires and
what makes the offboarding story a single statement. ``documents.connector_id`` cascades
too, so dropping a connector drops its rows — but that only removes the *database* half.
The objects and the vectors are removed by the delete job, which is why the connector is
marked ``deleting`` before anything is torn down rather than deleted outright.

``job_dead_letters`` has no ``organization_id``. It is an operations table, and giving it
a tenant key would oblige the worker to invent a tenant scope on a path where there is no
request and no session. See the model docstring; the org id lives in the payload.

``documents.source_uri`` is ``Text``, not a bounded string, and the unique constraint is
on ``(connector_id, source_uri)``. Object keys are long — a nested folder drop easily
passes 255 characters — and PostgreSQL's btree limit is on the *index tuple*, roughly
2704 bytes here, which no realistic key approaches. Truncating keys to fit a shorter type
would silently merge two documents into one row.

Revision ID: 0009_connectors
Revises: 0008_routing_modes
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_connectors"
down_revision: str | None = "0008_routing_modes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOCUMENT_STATUSES = (
    "pending",
    "extracting",
    "chunking",
    "embedding",
    "indexed",
    "failed",
    "skipped",
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False, server_default="managed_file_drop"),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("chunking", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("storage_prefix", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ready"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('managed_file_drop')", name="type_is_known"),
        sa.CheckConstraint(
            "status IN ('ready', 'syncing', 'deleting', 'error')",
            name="status_is_known",
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_connectors_organization_id_name"),
    )
    op.create_index("ix_connectors_organization_id", "connectors", ["organization_id"])
    op.create_index("ix_connectors_organization_id_id", "connectors", ["organization_id", "id"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(f"status IN ({_quoted(_DOCUMENT_STATUSES)})", name="status_is_known"),
        sa.CheckConstraint("size_bytes >= 0", name="size_is_not_negative"),
        sa.CheckConstraint("chunk_count >= 0", name="chunk_count_is_not_negative"),
        sa.UniqueConstraint(
            "connector_id", "source_uri", name="uq_documents_connector_id_source_uri"
        ),
    )
    op.create_index("ix_documents_organization_id", "documents", ["organization_id"])
    op.create_index("ix_documents_organization_id_id", "documents", ["organization_id", "id"])
    op.create_index("ix_documents_connector_id_id", "documents", ["connector_id", "id"])
    op.create_index("ix_documents_connector_id_status", "documents", ["connector_id", "status"])

    op.create_table(
        "job_dead_letters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("attempts > 0", name="attempts_is_positive"),
    )
    op.create_index("ix_job_dead_letters_job_name_id", "job_dead_letters", ["job_name", "id"])


def downgrade() -> None:
    op.drop_table("job_dead_letters")
    op.drop_table("documents")
    op.drop_table("connectors")
