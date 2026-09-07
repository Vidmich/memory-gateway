"""Proxy core: organizations, upstream models, gateways, targets, API keys.

The minimum schema the data plane needs to authenticate a key and forward a completion.
``organizations`` carries only identity here; task 04 fills in tenancy, and task 08 gives
``gateway_targets.priority`` / ``weight`` meaning beyond a single target.

Revision ID: 0002_proxy_core
Revises: 0001_baseline
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_proxy_core"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Check constraints are named short here on purpose: the metadata naming convention in
# app/db/base.py expands `%(constraint_name)s` into `ck_<table>_<name>`, and spelling the
# full name out would produce `ck_organizations_ck_organizations_...`.
_TIMESTAMP = sa.DateTime(timezone=True)
_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name="status_is_known",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
    )

    op.create_table(
        "upstream_models",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("dialect", sa.String(length=32), nullable=False),
        sa.Column("upstream_model_id", sa.Text(), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("extra_headers", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("system_context", sa.Text(), nullable=True),
        sa.Column("default_params", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "scope IN ('global', 'org')",
            name="scope_is_known",
        ),
        sa.CheckConstraint(
            "dialect IN ('openai', 'anthropic')",
            name="dialect_is_known",
        ),
        sa.CheckConstraint(
            "auth_type IN ('bearer', 'api_key_header', 'azure', 'none')",
            name="auth_type_is_known",
        ),
        sa.CheckConstraint(
            "timeout_seconds > 0",
            name="timeout_is_positive",
        ),
        sa.CheckConstraint(
            "(scope = 'global' AND organization_id IS NULL)"
            " OR (scope = 'org' AND organization_id IS NOT NULL)",
            name="scope_matches_organization",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_upstream_models_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_upstream_models"),
        sa.UniqueConstraint(
            "organization_id",
            "name",
            name="uq_upstream_models_organization_id_name",
        ),
    )
    # A NULL organization_id never collides in a UNIQUE constraint, so global model names
    # need a partial index of their own to stay unique.
    op.create_index(
        "uq_upstream_models_global_name",
        "upstream_models",
        ["name"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )

    op.create_table(
        "gateways",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("system_context", sa.Text(), nullable=True),
        sa.Column("param_overrides", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_gateways_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_gateways"),
        sa.UniqueConstraint("slug", name="uq_gateways_slug"),
    )
    op.create_index("ix_gateways_organization_id", "gateways", ["organization_id"])

    op.create_table(
        "gateway_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gateway_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("upstream_model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.CheckConstraint("weight >= 0", name="weight_is_not_negative"),
        sa.ForeignKeyConstraint(
            ["gateway_id"],
            ["gateways.id"],
            name="fk_gateway_targets_gateway_id_gateways",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_model_id"],
            ["upstream_models.id"],
            name="fk_gateway_targets_upstream_model_id_upstream_models",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_gateway_targets"),
        sa.UniqueConstraint("gateway_id", "upstream_model_id", name="uq_gateway_targets_pair"),
    )
    op.create_index("ix_gateway_targets_gateway_id", "gateway_targets", ["gateway_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gateway_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("last_used_at", _TIMESTAMP, nullable=True),
        sa.Column("revoked_at", _TIMESTAMP, nullable=True),
        sa.Column("created_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.Column("updated_at", _TIMESTAMP, server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["gateway_id"],
            ["gateways.id"],
            name="fk_api_keys_gateway_id_gateways",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_gateway_id", "api_keys", ["gateway_id"])


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("gateway_targets")
    op.drop_table("gateways")
    op.drop_index("uq_upstream_models_global_name", table_name="upstream_models")
    op.drop_table("upstream_models")
    op.drop_table("organizations")
