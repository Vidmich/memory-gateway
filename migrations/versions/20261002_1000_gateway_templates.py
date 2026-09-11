"""Task 105: request and response templates per gateway, and the template fingerprint.

``gateways.template_config`` is the fourth configuration blob, ``'{}'`` for every existing
row — which loads as the defaults, which are the exact strings SPEC §7 has always printed,
so no gateway renders differently after this migration.

``request_logs.template_fingerprint`` records which set of templates assembled the prompt
and wrapped the answer, so a change of wording is a visible boundary in the log and a
filter on the Monitoring screen. Nullable and not backfilled: a row written before this
column existed was rendered by the defaults, but saying so would be a guess dressed as a
record. Raw SQL like every other column added to ``request_logs``: the table is partitioned.

Revision ID: 0024_gateway_templates
Revises: 0023_reprocessing_status
Create Date: 2026-10-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0024_gateway_templates"
down_revision: str | None = "0023_reprocessing_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gateways",
        sa.Column("template_config", JSONB, nullable=False, server_default="{}"),
    )
    op.execute("ALTER TABLE request_logs ADD COLUMN template_fingerprint varchar(16)")


def downgrade() -> None:
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS template_fingerprint")
    op.drop_column("gateways", "template_config")
