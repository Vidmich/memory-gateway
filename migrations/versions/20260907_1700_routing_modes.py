"""Failover and A/B split: the flag SPEC §8.2 names, on the request log.

One column, and the reason it is a column rather than another error code is worth
recording. A stream that dies after its first chunk leaves three separate facts behind:
the status was 200 (it was — the status line went out before anything failed), the error
was ``stream_failed``, and *failover could not have helped* because the response had
already begun. The third is the one an operator acts on: it is the difference between
"add a second target" and "there was nothing this gateway could have done". Folding it
into either of the other two would lose it.

``ADD COLUMN`` on a declaratively partitioned parent propagates to every existing and
future partition, so this touches ``request_logs`` and each day's table in one statement.
It is also metadata-only on PostgreSQL 11 and later even with ``NOT NULL DEFAULT``, so it
does not rewrite the log table while the proxy is writing to it.

The per-target weights this task also needs already exist: ``gateway_targets.weight`` was
created in ``0006_gateways_keys`` with a ``weight >= 0`` check, precisely so enabling A/B
would be code rather than a migration on a live table.

Revision ID: 0008_routing_modes
Revises: 0007_request_logging
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_routing_modes"
down_revision: str | None = "0007_request_logging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE request_logs "
        "ADD COLUMN failed_after_stream_start boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS failed_after_stream_start")
