"""Gateways: the full configuration model, and key expiry.

Five columns and a CHECK. The three ``*_config`` blobs are added empty rather than with a
JSON default, because the defaults live in :mod:`app.schemas.gateway_config` and a value
duplicated into a ``server_default`` would be a second copy to keep in step — one that
older rows would silently disagree with the moment task 10 adds a field.

``slug_is_url_safe`` is the one constraint worth arguing about. The service validates the
same shape with a readable message, so this never fires for a UI user; it exists because
the slug is a path segment on a public URL and a row written by a script, a fixture, or a
future endpoint that skipped the service would break routing rather than fail a write.
The existing rows are all ``demo``-shaped, so there is nothing to clean up first.

Revision ID: 0006_gateways_keys
Revises: 0005_model_catalog
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_gateways_keys"
down_revision: str | None = "0005_model_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOBS = ("locked_params", "memory_config", "logging_config", "limits")


def upgrade() -> None:
    op.add_column(
        "gateways",
        sa.Column(
            "routing_mode",
            sa.String(length=16),
            nullable=False,
            server_default="single",
        ),
    )
    for name in _BLOBS:
        op.add_column(
            "gateways",
            sa.Column(
                name,
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
        )

    op.create_check_constraint(
        "routing_mode_is_known",
        "gateways",
        "routing_mode IN ('single', 'failover', 'ab_split')",
    )
    op.create_check_constraint(
        "slug_is_url_safe",
        "gateways",
        r"slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])$'",
    )
    # `organization_id` leads, as on every composite index here, so the scoped list is
    # one index scan and row-level security stays mechanical to add later.
    op.create_index("ix_gateways_organization_id_id", "gateways", ["organization_id", "id"])

    op.add_column(
        "api_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "expires_at")
    op.drop_index("ix_gateways_organization_id_id", table_name="gateways")
    op.drop_constraint("slug_is_url_safe", "gateways", type_="check")
    op.drop_constraint("routing_mode_is_known", "gateways", type_="check")
    for name in reversed(_BLOBS):
        op.drop_column("gateways", name)
    op.drop_column("gateways", "routing_mode")
