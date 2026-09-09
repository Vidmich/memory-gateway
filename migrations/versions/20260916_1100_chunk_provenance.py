"""How each document was cut, recorded on the row (task 20).

Two nullable columns and no backfill, for the same reason ``embedding_model`` was left
nullable when SPEC §9.4 introduced it: a value written now would be a *guess* about a
document indexed before per-format overrides existed, and a guess in a drift-detection
column is worse than a blank. ``NULL`` reads as "cut before this was recorded", which is
exactly what it means, and the next ingestion of that document fills it in truthfully.

The reason there are two columns rather than one is that they answer different questions.
``chunk_strategy`` is for a person: it is what the document list shows next to a file
whose chunks look wrong. ``chunk_fingerprint`` is for a comparison: it covers every field
of the effective configuration, plus the embedding model for the strategies whose
boundaries come out of it, so "is this document's chunking still current" is one equality
test rather than a re-derivation of the override resolution.

Revision ID: 0018_chunk_provenance
Revises: 0017_vector_bindings
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_chunk_provenance"
down_revision: str | None = "0017_vector_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("chunk_strategy", sa.String(length=32), nullable=True))
    op.add_column("documents", sa.Column("chunk_fingerprint", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "chunk_fingerprint")
    op.drop_column("documents", "chunk_strategy")
