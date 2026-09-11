"""Task 102: document summarization before chunking and embedding.

Three things. A ``summarization`` blob on the connector beside ``chunking``; the summary and
its provenance on the document row; and ``summarization_runs``, the ledger — one row per
attempt, with the provider's token counts, which is the column ``distillation_runs`` never
had and the roadmap's cost accounting needs.

Revision ID: 0021_document_summarization
Revises: 0020_tokenizer_follows_model
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0021_document_summarization"
down_revision: str | None = "0020_tokenizer_follows_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column("summarization", JSONB, nullable=False, server_default="{}"),
    )
    # A new phase between extraction and chunking. Swapped the way task 17 widened the
    # organization statuses: the CHECK is dropped and recreated with one more value.
    op.execute("ALTER TABLE documents DROP CONSTRAINT ck_documents_status_is_known")
    op.create_check_constraint(
        "status_is_known",
        "documents",
        "status IN ('pending', 'extracting', 'summarizing', 'chunking', 'embedding', "
        "'indexed', 'failed', 'skipped')",
    )

    op.add_column("documents", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("summary_status", sa.String(length=16), nullable=True))
    op.add_column("documents", sa.Column("summary_error", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("summary_model", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("summary_model_id", PGUUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("summary_prompt_version", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("summary_tokens_in", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("summary_tokens_out", sa.Integer(), nullable=True))
    op.add_column(
        "documents", sa.Column("summarized_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "summarization_runs",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("document_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("model_id", PGUUID(as_uuid=True), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'skipped')", name="outcome_is_known"
        ),
        sa.CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="tokens_are_not_negative"),
    )
    op.create_index(
        "ix_summarization_runs_organization_id", "summarization_runs", ["organization_id"]
    )
    op.create_index(
        "ix_summarization_runs_organization_id_created_at",
        "summarization_runs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_summarization_runs_connector_id_created_at",
        "summarization_runs",
        ["connector_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_summarization_runs_connector_id_created_at", table_name="summarization_runs")
    op.drop_index(
        "ix_summarization_runs_organization_id_created_at", table_name="summarization_runs"
    )
    op.drop_index("ix_summarization_runs_organization_id", table_name="summarization_runs")
    op.drop_table("summarization_runs")
    for column in (
        "summarized_at",
        "summary_tokens_out",
        "summary_tokens_in",
        "summary_prompt_version",
        "summary_model_id",
        "summary_model",
        "summary_error",
        "summary_status",
        "summary",
    ):
        op.drop_column("documents", column)
    # Any row caught mid-phase has to be given a value the old CHECK accepts first.
    op.execute("UPDATE documents SET status = 'chunking' WHERE status = 'summarizing'")
    op.execute("ALTER TABLE documents DROP CONSTRAINT ck_documents_status_is_known")
    op.create_check_constraint(
        "status_is_known",
        "documents",
        "status IN ('pending', 'extracting', 'chunking', 'embedding', 'indexed', "
        "'failed', 'skipped')",
    )
    op.drop_column("connectors", "summarization")
