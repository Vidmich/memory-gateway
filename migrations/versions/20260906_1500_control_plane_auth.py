"""Control-plane auth: users and refresh-token sessions.

Adds the two tables the UI logs in against. ``users.role`` already carries all four roles
from SPEC §5.2 — task 04 enforces them, and widening a CHECK on a populated table later
would mean a validation scan under lock.

Revision ID: 0003_control_plane_auth
Revises: 0002_proxy_core
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_control_plane_auth"
down_revision: str | None = "0002_proxy_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Check constraints are named short here on purpose: the metadata naming convention in
# app/db/base.py expands `%(constraint_name)s` into `ck_<table>_<name>`.
_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("now()")


def upgrade() -> None:
    # CITEXT gives case-insensitive uniqueness on the email column itself, so no insert
    # path can bypass it by forgetting to lower-case first.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_login_at", _TIMESTAMP, nullable=True),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "role IN ('superadmin', 'org_admin', 'org_member', 'org_viewer')",
            name="role_is_known",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'invited', 'suspended')",
            name="status_is_known",
        ),
        sa.CheckConstraint(
            "(role = 'superadmin' AND organization_id IS NULL)"
            " OR (role <> 'superadmin' AND organization_id IS NOT NULL)",
            name="role_matches_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_users_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_organization_id", "users", ["organization_id"])

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", _TIMESTAMP, nullable=False),
        sa.Column("persistent", sa.Boolean(), nullable=False),
        sa.Column("replaced_at", _TIMESTAMP, nullable=True),
        sa.Column("revoked_at", _TIMESTAMP, nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("refresh_token_hash", name="uq_sessions_refresh_token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    # Reuse detection revokes a whole family at once, so that lookup has to be indexed.
    op.create_index("ix_sessions_family_id", "sessions", ["family_id"])


def downgrade() -> None:
    op.drop_table("sessions")
    op.drop_index("ix_users_organization_id", table_name="users")
    op.drop_table("users")
    # The citext extension is left in place: dropping it would fail if anything else in
    # the database has come to depend on it, and an unused extension costs nothing.
