"""Task 104: reprocessing status — the index fingerprint, the second status axis, and runs.

Three columns on ``documents`` and one table. ``index_fingerprint`` is the *expand* half of
an expand-contract: ``chunk_fingerprint`` stays and is still written this release, the
readers move to the new column, and the old one is dropped by the next migration. No
backfill, on purpose and for the same reason task 20 gave: the old digest cannot be turned
into the new segments, and a ``NULL`` means *unrecorded* — shown as such, reprocessable on
request, and never counted as stale.

``index_status`` is the stored index over the fingerprint comparison: ``current`` for every
existing row, because nothing can be known to be stale before it has a fingerprint to be
compared. ``reprocessing_run_id`` marks the documents a run owns, so a run a dead worker
abandoned is continued over exactly those.

``reprocessing_runs`` is one row per tracked recut of a connector; see the model.

Revision ID: 0023_reprocessing_status
Revises: 0022_retrieval_validation
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0023_reprocessing_status"
down_revision: str | None = "0022_retrieval_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("index_fingerprint", sa.String(length=128), nullable=True))
    op.add_column(
        "documents",
        sa.Column("index_status", sa.String(length=16), nullable=False, server_default="current"),
    )
    op.add_column(
        "documents", sa.Column("reprocessing_run_id", PGUUID(as_uuid=True), nullable=True)
    )
    op.create_check_constraint(
        "index_status_is_known",
        "documents",
        "index_status IN ('current', 'stale', 'reprocessing')",
    )
    op.create_index(
        "ix_documents_connector_id_index_status", "documents", ["connector_id", "index_status"]
    )

    op.create_table(
        "reprocessing_runs",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("formats", JSONB, nullable=False, server_default="[]"),
        sa.Column("requested_by", PGUUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_label", sa.Text(), nullable=True),
        sa.Column("reindex_run_id", PGUUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("spent_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("resumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('chunking', 'embedding_model', 'tokenizer', 'summarization',"
            " 'extractor', 'manual')",
            name="trigger_is_known",
        ),
        sa.CheckConstraint(
            "scope IN ('stale', 'formats', 'all', 'unrecorded', 'failed')",
            name="scope_is_known",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')", name="status_is_known"
        ),
        sa.CheckConstraint(
            "total >= 0 AND done >= 0 AND failed >= 0 AND skipped >= 0",
            name="counters_are_not_negative",
        ),
    )
    op.create_index(
        "ix_reprocessing_runs_organization_id", "reprocessing_runs", ["organization_id"]
    )
    op.create_index(
        "ix_reprocessing_runs_connector_id_started_at",
        "reprocessing_runs",
        ["connector_id", "started_at"],
    )
    op.create_index("ix_reprocessing_runs_reindex_run_id", "reprocessing_runs", ["reindex_run_id"])


def downgrade() -> None:
    op.drop_index("ix_reprocessing_runs_reindex_run_id", table_name="reprocessing_runs")
    op.drop_index("ix_reprocessing_runs_connector_id_started_at", table_name="reprocessing_runs")
    op.drop_index("ix_reprocessing_runs_organization_id", table_name="reprocessing_runs")
    op.drop_table("reprocessing_runs")
    op.drop_index("ix_documents_connector_id_index_status", table_name="documents")
    op.drop_constraint("index_status_is_known", "documents", type_="check")
    op.drop_column("documents", "reprocessing_run_id")
    op.drop_column("documents", "index_status")
    op.drop_column("documents", "index_fingerprint")
