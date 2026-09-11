"""Which injected chunks the answer cited (task 100).

Two columns beside ``retrieved_chunk_ids``. That column is what went *into* the prompt;
``cited_chunk_ids`` is what the answer *used*, and the ratio between them is the only
relevance signal the product gets without somebody labelling a set — a chunk injected on
every request and cited on none is a retrieval false positive. ``citations_unresolved``
counts the handles that pointed at nothing: a ``[7]`` when six were injected.

Both have server defaults so the existing partitions read as "nothing cited, nothing
unresolved", which is honest for rows written before the gateway looked. The two columns
are metadata and follow the metadata retention, like every other column on this table.

Revision ID: 0019_answer_citations
Revises: 0018_chunk_provenance
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_answer_citations"
down_revision: str | None = "0018_chunk_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Raw SQL like every other column added to this table: it is partitioned, and a
    # default on the parent is what makes the existing partitions read as "nothing cited".
    op.execute(
        "ALTER TABLE request_logs ADD COLUMN cited_chunk_ids jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE request_logs ADD COLUMN citations_unresolved integer NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS citations_unresolved")
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS cited_chunk_ids")
