"""Tenancy: organization settings and invitations.

``organizations.settings`` gets a ``'{}'`` server default so the column can be NOT NULL
without a rewrite pass over existing rows, and so a later task can read it without
handling ``None``.

Revision ID: 0004_tenancy
Revises: 0003_control_plane_auth
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_tenancy"
down_revision: str | None = "0003_control_plane_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "settings",
            postgresql.JSONB(),
            server_default="{}",
            nullable=False,
        ),
    )

    op.create_table(
        "invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", _TIMESTAMP, nullable=False),
        sa.Column("accepted_at", _TIMESTAMP, nullable=True),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        # Named bare: the metadata naming convention in app/db/base.py expands it
        # into `ck_invitations_role_is_invitable`, and spelling the full name here
        # would expand it twice.
        sa.CheckConstraint(
            "role IN ('org_admin', 'org_member', 'org_viewer')",
            name="role_is_invitable",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_invitations_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name="fk_invitations_invited_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
    )
    # Partial: an accepted invitation must not block a fresh one to the same address.
    op.create_index(
        "uq_invitations_pending_email",
        "invitations",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL"),
    )
    # organization_id leads, so the scoped list query uses it and row-level security
    # stays mechanical to add later.
    op.create_index(
        "ix_invitations_organization_id_created_at",
        "invitations",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_organization_id_created_at", table_name="invitations")
    op.drop_index("uq_invitations_pending_email", table_name="invitations")
    op.drop_table("invitations")
    op.drop_column("organizations", "settings")
