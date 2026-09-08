"""``request_logs.dropped_params`` — what the dialect could not carry.

Task 16 makes Claude a usable upstream, and translating between two providers' request
formats is not lossless: OpenAI's ``presence_penalty``, ``frequency_penalty``, ``seed``,
``logit_bias`` and ``response_format`` have no Anthropic equivalent, so they do not go on
the wire.

The failure this column prevents is a quiet one. A request that drops a parameter still
succeeds, still returns a completion, and gives no sign anywhere that what was asked for
was not done — the caller concludes the parameter has no effect on this model, which is
almost the truth and entirely unactionable. Recording the set per request turns that into
a question the request drawer answers.

Empty for every OpenAI-shaped upstream, which is most rows. A JSONB array rather than a
text column because the reader is a list and the writer is a list, and because the next
dialect will drop a different set.

``ADD COLUMN`` on a declaratively partitioned parent propagates to every existing and
future partition in one statement, and is metadata-only on PostgreSQL 11 and later even
with ``NOT NULL DEFAULT`` — so it does not rewrite the log table while the proxy is
writing to it.

Revision ID: 0015_anthropic_dialect
Revises: 0014_audit_log
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_anthropic_dialect"
down_revision: str | None = "0014_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE request_logs ADD COLUMN dropped_params jsonb NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS dropped_params")
