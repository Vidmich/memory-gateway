"""``distillation_runs``, and the link from a retracted fact to the one that replaced it.

Two changes, both in service of the same thing: making an automatically written memory
explainable after the fact.

**``memory_facts.superseded_by_id``** turns a retraction into a pair. "Works in Rust" is
not merely switched off when somebody says they have moved to Go — it is *replaced*, and
the browser shows the two together. ``ON DELETE SET NULL`` rather than ``CASCADE``:
deleting the replacement must not delete the history it replaced.

**``distillation_runs``** is one row per pass. SPEC §10.1's memory-health signals are rates
over a selectable window — facts written per day, failure rate, dedupe rate, supersession
rate — and a window outlives the process that a Prometheus counter lives in. It is also
where the daily cost cap is counted from, so the number the Settings screen shows is the
number the guard actually used.

Unpartitioned, unlike ``request_logs``: the debounce means a forty-turn conversation is one
row, which is orders of magnitude below traffic. Task 17's retention job prunes it by age.

Revision ID: 0013_distillation
Revises: 0012_end_user_memory
Create Date: 2026-09-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_distillation"
down_revision: str | None = "0012_end_user_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_REASON_LENGTH = 500


def upgrade() -> None:
    op.add_column(
        "memory_facts",
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_memory_facts_superseded_by_id_memory_facts",
        "memory_facts",
        "memory_facts",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "distillation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "end_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("end_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=_MAX_REASON_LENGTH), nullable=True),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("transcripts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deduped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("superseded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evicted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'skipped')", name="outcome_is_known"
        ),
        sa.CheckConstraint(
            "candidates >= 0 AND inserted >= 0 AND deduped >= 0 AND superseded >= 0 "
            "AND evicted >= 0 AND rejected >= 0",
            name="counts_are_not_negative",
        ),
    )
    op.create_index(
        "ix_distillation_runs_organization_id", "distillation_runs", ["organization_id"]
    )
    op.create_index(
        "ix_distillation_runs_organization_id_created_at",
        "distillation_runs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_distillation_runs_end_user_id_created_at",
        "distillation_runs",
        ["end_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("distillation_runs")
    op.drop_constraint(
        "fk_memory_facts_superseded_by_id_memory_facts", "memory_facts", type_="foreignkey"
    )
    op.drop_column("memory_facts", "superseded_by_id")
