"""Per-organization vector backend bindings (task 19).

One table, and one deliberate omission: **no rows are backfilled.** Every organization
indexed before this migration is on Qdrant, and a row saying so would be true — but
writing it would freeze today's default into every existing tenant, so a deployment that
later changes its default would find that the change applies to nobody. An absent row
means "whatever this deployment's default backend is", resolved at read time, which is the
same precedence rule task 17 established for platform settings and for the same reason.

The consequence to be aware of: an operator who changes the platform default *does* move
unbound organizations, and moving an organization means its vectors are somewhere else. So
:mod:`app.services.vector_backends` resolves an unbound organization by writing the
binding on first use — the default decides where a tenant *starts*, and after that the row
decides, which is what makes the default safe to change.

Revision ID: 0017_vector_bindings
Revises: 0016_platform_maintenance
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_vector_bindings"
down_revision: str | None = "0016_platform_maintenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vector_bindings",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("backend", sa.String(length=32), nullable=False),
        sa.Column("collection", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="bound"),
        sa.Column("target", sa.String(length=32), nullable=True),
        sa.Column("target_collection", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Short names: the metadata's naming convention expands them to
        # `ck_vector_bindings_<name>`, and spelling the expanded form would expand twice.
        sa.CheckConstraint("backend IN ('qdrant', 'chroma')", name="backend_is_known"),
        sa.CheckConstraint(
            "target IS NULL OR target IN ('qdrant', 'chroma')", name="target_is_known"
        ),
        sa.CheckConstraint("status IN ('bound', 'migrating')", name="status_is_known"),
        sa.CheckConstraint(
            "(status = 'migrating') = (target IS NOT NULL)", name="migrating_has_a_target"
        ),
        sa.CheckConstraint("target IS NULL OR target <> backend", name="target_is_elsewhere"),
    )


def downgrade() -> None:
    op.drop_table("vector_bindings")
