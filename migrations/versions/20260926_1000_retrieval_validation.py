"""Task 103: validation — index audits, evaluation sets and runs.

Four tables and one column. ``index_audits`` stores the chunking and embedding reports per
connector; ``evaluation_sets`` / ``evaluation_items`` are a gateway's labelled questions;
``evaluation_runs`` is a measurement of a known state, with the configuration, the index
fingerprints and the per-item results on the row. ``summarization_runs.purpose`` tells the
ledger's summary rows from the evaluation rows that generating a question from a chunk
writes through the same chain.

Revision ID: 0022_retrieval_validation
Revises: 0021_document_summarization
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0022_retrieval_validation"
down_revision: str | None = "0021_document_summarization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _organization() -> sa.Column[object]:
    return sa.Column(
        "organization_id",
        PGUUID(as_uuid=True),
        sa.ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )


def _now(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.add_column(
        "summarization_runs",
        sa.Column("purpose", sa.String(length=16), nullable=False, server_default="summary"),
    )
    op.create_check_constraint(
        "purpose_is_known", "summarization_runs", "purpose IN ('summary', 'evaluation')"
    )

    op.create_table(
        "index_audits",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        _organization(),
        sa.Column(
            "connector_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("created_by", PGUUID(as_uuid=True), nullable=True),
        sa.Column("drift_sample", sa.Integer(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report", JSONB, nullable=False, server_default="{}"),
        sa.Column("severity", sa.String(length=8), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _now("created_at"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('chunking', 'embedding')", name="kind_is_known"),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status_is_known"),
    )
    op.create_index("ix_index_audits_organization_id", "index_audits", ["organization_id"])
    op.create_index(
        "ix_index_audits_connector_id_kind_created_at",
        "index_audits",
        ["connector_id", "kind", "created_at"],
    )

    op.create_table(
        "evaluation_sets",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        _organization(),
        sa.Column(
            "gateway_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("gateways.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", PGUUID(as_uuid=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
    )
    op.create_index("ix_evaluation_sets_organization_id", "evaluation_sets", ["organization_id"])
    op.create_index("ix_evaluation_sets_gateway_id", "evaluation_sets", ["gateway_id"])

    op.create_table(
        "evaluation_items",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        _organization(),
        sa.Column(
            "set_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("evaluation_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("relevant", JSONB, nullable=False, server_default="[]"),
        sa.Column("relevant_document_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "source IN ('log', 'citation', 'manual', 'generated')", name="source_is_known"
        ),
    )
    op.create_index("ix_evaluation_items_organization_id", "evaluation_items", ["organization_id"])
    op.create_index("ix_evaluation_items_set_id", "evaluation_items", ["set_id"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        _organization(),
        sa.Column(
            "set_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("evaluation_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gateway_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("created_by", PGUUID(as_uuid=True), nullable=True),
        sa.Column("patch", JSONB, nullable=True),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("snapshot", JSONB, nullable=False, server_default="{}"),
        sa.Column("metrics", JSONB, nullable=False, server_default="{}"),
        sa.Column("results", JSONB, nullable=False, server_default="[]"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        _now("created_at"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="status_is_known"
        ),
    )
    op.create_index("ix_evaluation_runs_organization_id", "evaluation_runs", ["organization_id"])
    op.create_index(
        "ix_evaluation_runs_set_id_created_at", "evaluation_runs", ["set_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_runs_set_id_created_at", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_organization_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_evaluation_items_set_id", table_name="evaluation_items")
    op.drop_index("ix_evaluation_items_organization_id", table_name="evaluation_items")
    op.drop_table("evaluation_items")
    op.drop_index("ix_evaluation_sets_gateway_id", table_name="evaluation_sets")
    op.drop_index("ix_evaluation_sets_organization_id", table_name="evaluation_sets")
    op.drop_table("evaluation_sets")
    op.drop_index("ix_index_audits_connector_id_kind_created_at", table_name="index_audits")
    op.drop_index("ix_index_audits_organization_id", table_name="index_audits")
    op.drop_table("index_audits")
    op.drop_constraint("ck_summarization_runs_purpose_is_known", "summarization_runs")
    op.drop_column("summarization_runs", "purpose")
