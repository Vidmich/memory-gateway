"""``upstream_models.context_window`` — the number the overflow guard needs.

Task 10's prompt assembler refuses to inject retrieved documents into a request whose own
messages already fill the model's context window, because the alternative is building a
request the provider will reject with a 400 that names a token count rather than a cause.
That guard needs a window, and there is no sound way to derive one: it differs by two
orders of magnitude across providers and changes when a provider ships a new revision of
the same model name.

So it is a column, it is nullable, and ``NULL`` means **unknown** rather than unlimited.
A model whose window nobody has stated skips the guard entirely — which is the same
behaviour as before this column existed, and is the right default, because a guessed
window would start refusing to inject memory into requests that would have been served
perfectly well.

Nullable also makes this migration a metadata-only ``ADD COLUMN``: no table rewrite, no
backfill, no lock worth naming on a table this size.

Revision ID: 0010_context_window
Revises: 0009_connectors
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_context_window"
down_revision: str | None = "0009_connectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("upstream_models", sa.Column("context_window", sa.Integer(), nullable=True))
    # Short name: the naming convention expands it to
    # `ck_upstream_models_context_window_is_positive`.
    op.create_check_constraint(
        "context_window_is_positive",
        "upstream_models",
        "context_window IS NULL OR context_window > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_upstream_models_context_window_is_positive", "upstream_models", type_="check"
    )
    op.drop_column("upstream_models", "context_window")
