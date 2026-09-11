"""The tokenizer follows the model (task 101).

Four nullable columns, no backfill. ``upstream_models.tokenizer`` is an override — ``NULL``
means *derived from the dialect and model id*, which is what every existing row meant
before the column existed. ``documents.tokenizer`` records what a document was cut with,
beside ``chunk_strategy`` and ``chunk_fingerprint``, for the reason task 20 gave for those:
a guess in a drift-detection column is worse than a blank. The two ``request_logs`` columns
carry our own prompt-token estimate and the tokenizer that made it, so the ratio against
the ``prompt_tokens`` the provider reports — the calibration — is a query over rows rather
than a table that has to be kept.

The embedding tokenizer needs no column: it lives in the ``embedding`` section of
``platform_settings``, whose blob is permissive on load.

Revision ID: 0020_tokenizer_follows_model
Revises: 0019_answer_citations
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020_tokenizer_follows_model"
down_revision: str | None = "0019_answer_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("upstream_models", sa.Column("tokenizer", JSONB, nullable=True))
    op.add_column("documents", sa.Column("tokenizer", sa.String(length=64), nullable=True))
    # Raw SQL like every other column added to request_logs: it is partitioned.
    op.execute("ALTER TABLE request_logs ADD COLUMN tokenizer varchar(64)")
    op.execute("ALTER TABLE request_logs ADD COLUMN estimated_prompt_tokens integer")


def downgrade() -> None:
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS estimated_prompt_tokens")
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS tokenizer")
    op.drop_column("documents", "tokenizer")
    op.drop_column("upstream_models", "tokenizer")
