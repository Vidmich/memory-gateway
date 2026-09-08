"""Platform settings, the maintenance jobs' records, and reindex runs (task 17).

Four ordinary tables and nothing clever, which is worth saying because the *interesting*
half of task 17 is DDL these tables describe rather than DDL they contain: retention
attaches and drops daily partitions of ``request_logs`` and ``transcripts`` at runtime,
so the partition management is in :mod:`app.services.maintenance` where it can be run
nightly, not here where it would run once.

``platform_settings`` starts **empty**. That is the precedence rule made concrete: an
environment variable is the bootstrap value, a row here overrides it, and seeding rows
from the environment at migration time would freeze whatever the machine running the
migration happened to have configured — which is usually a CI container.

Revision ID: 0016_platform_maintenance
Revises: 0015_anthropic_dialect
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_platform_maintenance"
down_revision: str | None = "0015_anthropic_dialect"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Constraint names here are the *short* ones the models use. The metadata's naming
# convention expands them to ``ck_<table>_<name>``; spelling the expanded form would
# make it expand twice, and the ``DROP CONSTRAINT`` that eventually needs it would miss.
def upgrade() -> None:
    # SPEC §6.5's organization deletion, as two facts on the row that already carries the
    # tenant's lifecycle. ``deleting`` behaves exactly like ``suspended`` everywhere that
    # already branches on ``status`` — no gateway serves, no member signs in — so the
    # widening needs no other code to change for the organization to go quiet.
    op.execute("ALTER TABLE organizations DROP CONSTRAINT ck_organizations_status_is_known")
    op.create_check_constraint(
        "status_is_known", "organizations", "status IN ('active', 'suspended', 'deleting')"
    )
    op.add_column(
        "organizations", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "maintenance_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("report", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "job IN ('partitions', 'retention', 'sweep')",
            name="job_is_known",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="status_is_known",
        ),
    )
    op.create_index("ix_maintenance_runs_job_started_at", "maintenance_runs", ["job", "started_at"])

    op.create_table(
        "reindex_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="platform"),
        sa.Column("scope_organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("from_model", sa.Text(), nullable=True),
        sa.Column("from_dimension", sa.Integer(), nullable=True),
        sa.Column("to_model", sa.Text(), nullable=False),
        sa.Column("to_dimension", sa.Integer(), nullable=False),
        sa.Column("estimated_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("scope IN ('platform', 'organization')", name="scope_is_known"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="status_is_known",
        ),
        sa.CheckConstraint("to_dimension > 0", name="dimension_is_positive"),
    )
    op.create_index("ix_reindex_runs_status_started_at", "reindex_runs", ["status", "started_at"])

    op.create_table(
        "reindex_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("total_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["reindex_runs.id"],
            name="fk_reindex_targets_run_id_reindex_runs",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'embedding', 'verifying', 'swapped', 'failed')",
            name="status_is_known",
        ),
        sa.CheckConstraint("done_points >= 0", name="done_is_not_negative"),
        sa.UniqueConstraint(
            "run_id", "organization_id", name="uq_reindex_targets_run_organization"
        ),
    )
    op.create_index("ix_reindex_targets_run_id_id", "reindex_targets", ["run_id", "id"])


def downgrade() -> None:
    op.drop_column("organizations", "purge_after")
    op.execute("ALTER TABLE organizations DROP CONSTRAINT ck_organizations_status_is_known")
    op.create_check_constraint(
        "status_is_known", "organizations", "status IN ('active', 'suspended')"
    )
    op.drop_table("reindex_targets")
    op.drop_table("reindex_runs")
    op.drop_table("maintenance_runs")
    op.drop_table("platform_settings")
