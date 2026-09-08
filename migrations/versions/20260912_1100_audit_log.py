"""``audit_events`` — the immutable record of every control-plane mutation (SPEC §10.4).

One table and one trigger. The trigger is the part worth explaining.

"Append-only" is a property the application cannot give you: the repository exposes no
update path today, and the next person to add one will not know that was load-bearing.
So the rule is stated where it cannot be forgotten — a ``BEFORE UPDATE OR DELETE`` trigger
that raises — and it holds for the ORM, for ``psql``, and for a migration written in a
hurry at two in the morning.

It is a backstop, not a vault: the table's owner can drop the trigger, and an operator
restoring from a backup can do anything at all. The deployment-level half is a grant —
``SELECT, INSERT`` and nothing else for the application role — which task 18 folds into
the hardened database setup, and true tamper-evidence (hash chaining, shipping the log
somewhere the operator cannot rewrite) is deliberately deferred to the same place. What
this trigger buys today is that no code path in this application can quietly rewrite
history, which is the failure that actually happens.

No foreign keys at all: the log has to survive its subjects. Removing a member deletes
their ``users`` row, and an audit trail whose actor becomes ``NULL`` is worth little in
the investigation it exists for — the labels are what keep those rows readable.
``organization_id`` is included in that, deliberately: a cascade from ``organizations``
would also be a ``DELETE`` this trigger refuses, so the choice was between a table that
cannot be tidied up and one whose customer record disappears with the customer, and
neither of those is what a log is for.

Retention is task 17's, and it prunes by ``created_at`` with a ``DELETE``, which this
trigger would refuse. That is why the trigger names the exception rather than being a
blanket ``RAISE``: the retention job runs as a role that may drop the trigger for the
duration, and the alternative — partitioning this table like ``request_logs`` — buys a
cheap prune for a table that gains a few thousand rows a year.

Revision ID: 0014_audit_log
Revises: 0013_distillation
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_audit_log"
down_revision: str | None = "0013_distillation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION audit_events_are_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (attempted %)', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(length=200), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False, server_default="user"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_label", sa.String(length=200), nullable=True),
        sa.Column("diff", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "actor_type IN ('user', 'system', 'superadmin_impersonation')",
            name="actor_type_is_known",
        ),
    )
    op.create_index(
        "ix_audit_events_organization_id_created_at",
        "audit_events",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_actor_user_id_created_at",
        "audit_events",
        ["actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_target_type_target_id",
        "audit_events",
        ["target_type", "target_id"],
    )
    op.create_index("ix_audit_events_organization_id_id", "audit_events", ["organization_id", "id"])

    op.execute(_APPEND_ONLY)
    op.execute(
        "CREATE TRIGGER audit_events_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_are_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_are_append_only()")
    op.drop_table("audit_events")
