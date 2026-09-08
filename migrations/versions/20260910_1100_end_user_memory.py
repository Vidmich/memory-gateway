"""``end_users`` and ``memory_facts`` — who is asking, and what we remember about them.

Two tables, both tenant-keyed and both cascading from ``organizations``, so offboarding
stays a single statement. The Qdrant side of conversation memory —
``org_{org_id}_memory`` — has no DDL: collections are created on first write, the same way
the document collection is.

Three choices here are worth reading before changing them.

**``end_users.external_id`` is a bounded string, not text.** It is the conflict target of
the upsert that creates rows on the request path, so it is a btree key on every request
that carries an identity. Two hundred characters holds a UUID, an email or an opaque
provider subject; a caller who sends more gets truncated by the service before it arrives
here, which is a bound the database also states rather than trusting.

**``memory_facts.source_log_id`` has no foreign key.** It points into ``request_logs``,
which is partitioned by day and dropped by retention (task 17). A fact learned from a
conversation must outlive the transcript it was learned from, and a cascade here would
erase the memory the moment the log window rolled over.

**The live index is partial.** Recall never reads a superseded fact, and superseded rows
accumulate for as long as the organization exists — so the index that recall uses stays
the size of the current memory rather than of everything ever believed.

Revision ID: 0012_end_user_memory
Revises: 0011_document_extraction
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_end_user_memory"
down_revision: str | None = "0011_document_extraction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FACT_KINDS = ("preference", "fact", "goal", "constraint")
_MAX_EXTERNAL_ID_LENGTH = 200
_MAX_FACT_LENGTH = 1000


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "end_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=_MAX_EXTERNAL_ID_LENGTH), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("request_count >= 0", name="request_count_is_not_negative"),
        sa.UniqueConstraint(
            "organization_id", "external_id", name="uq_end_users_organization_id_external_id"
        ),
    )
    op.create_index("ix_end_users_organization_id", "end_users", ["organization_id"])
    op.create_index("ix_end_users_organization_id_id", "end_users", ["organization_id", "id"])

    op.create_table(
        "memory_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "end_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("end_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="fact"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("source_log_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(f"kind IN ({_quoted(_FACT_KINDS)})", name="kind_is_known"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_is_a_fraction"),
        sa.CheckConstraint("length(text) > 0", name="text_is_not_empty"),
        sa.CheckConstraint(f"length(text) <= {_MAX_FACT_LENGTH}", name="text_is_not_too_long"),
    )
    op.create_index("ix_memory_facts_organization_id", "memory_facts", ["organization_id"])
    op.create_index("ix_memory_facts_organization_id_id", "memory_facts", ["organization_id", "id"])
    op.create_index("ix_memory_facts_end_user_id_id", "memory_facts", ["end_user_id", "id"])
    op.create_index(
        "ix_memory_facts_live",
        "memory_facts",
        ["end_user_id", "last_seen_at"],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("memory_facts")
    op.drop_table("end_users")
