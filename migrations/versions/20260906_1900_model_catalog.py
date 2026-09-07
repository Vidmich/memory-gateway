"""Model catalog: the stored credential hint, and an index for the scoped list.

``credential_hint`` is the ``sk-...4f2a`` display form, derived from the plaintext when
the credential is written. Storing it means the Models list renders without touching the
master key — and that a row written under a key that has since been rotated still shows
something rather than failing to decrypt on a read-only screen.

Nullable with no backfill: rows seeded before this migration have a credential but no
hint, and the API reports them as configured with no hint rather than inventing one.
Decrypting every row here to compute it would put every provider key through a migration,
which is the one place they have no business being.

Revision ID: 0005_model_catalog
Revises: 0004_tenancy
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_model_catalog"
down_revision: str | None = "0004_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upstream_models",
        sa.Column("credential_hint", sa.String(length=64), nullable=True),
    )
    # `organization_id` leads so the scoped `WHERE organization_id = ? ORDER BY id DESC`
    # is one index scan, and so row-level security stays mechanical to add later.
    op.create_index(
        "ix_upstream_models_organization_id_id",
        "upstream_models",
        ["organization_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_upstream_models_organization_id_id", table_name="upstream_models")
    op.drop_column("upstream_models", "credential_hint")
