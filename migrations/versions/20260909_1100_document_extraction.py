"""``documents.reason`` and ``documents.page_count`` — what the UI needs to explain itself.

Task 11 adds formats that fail in ways a customer can act on. A scanned PDF is not broken;
it needs OCR, which this build does not have. A password-protected file is not corrupt; it
needs an unprotected copy. Both already have a sentence in ``documents.error``, and a
sentence is the wrong thing for the UI to branch on — it is written for a person and gets
rewritten as the wording improves. ``reason`` is the same fact as a stable code, so
``needs_ocr`` can be a state with an explanation and a way out of it rather than a red row
with a paragraph in it.

Deliberately **not** constrained by a CHECK. The status column has one because its values
are a closed set the whole application agrees on; reasons are open by design — a new
extractor should be able to explain a new failure without a migration, and a code the UI
does not recognise degrades to showing the sentence, which is what it does today.

``page_count`` is the format's own unit: pages for a PDF, slides for a deck, sheets for a
workbook. The noun is derived in the UI from the media type rather than stored, because it
is a fact about the format and storing it would be a second column that can disagree with
the first. ``NULL`` where the format has no such unit — a Word document's pagination is
decided by the renderer, so any number here would be invented.

Both nullable, so this is a metadata-only ``ADD COLUMN`` twice over: no rewrite, no
backfill, and existing rows read as "nothing was recorded", which is true.

Revision ID: 0011_document_extraction
Revises: 0010_context_window
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_document_extraction"
down_revision: str | None = "0010_context_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("reason", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("page_count", sa.Integer(), nullable=True))
    # Short name: the naming convention expands it to
    # `ck_documents_page_count_is_not_negative`.
    op.create_check_constraint(
        "page_count_is_not_negative",
        "documents",
        "page_count IS NULL OR page_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_documents_page_count_is_not_negative", "documents", type_="check")
    op.drop_column("documents", "page_count")
    op.drop_column("documents", "reason")
