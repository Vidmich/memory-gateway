"""The audit log: one immutable row per control-plane mutation (SPEC §10.4).

Three properties this table is built around, and each of them costs something.

**Append-only.** There is no ``updated_at`` and no repository method that writes to an
existing row. The mixin is deliberately not :class:`~app.db.base.TimestampMixin`, because
that column would be a place for an ``UPDATE`` to hide. In production the guarantee is a
database grant — ``SELECT, INSERT`` and nothing else on this table for the application
role, which ``deploy/`` sets and task 18 hardens — because a convention holds only until
somebody writes the line that breaks it, and by then the log has already been trusted.

**It outlives its subjects.** There is not a foreign key on this table — not on
``actor_user_id``, not on ``target_id``, not even on ``organization_id``. That is unusual
here and it is the point: removing a member deletes their ``users`` row, and an audit
trail whose actor silently becomes ``NULL`` is worth very little in the investigation it
exists for. ``actor_label`` and ``target_label`` hold the email and the human name *as
they were at the time*, so the row stays readable when neither row exists any more — and
stays truthful when the name has since changed. ``organization_id`` follows the same rule
rather than cascading, because "the customer left, so their record of what we did to
their data is gone" is not a property anybody wants to discover afterwards.

**Nothing sensitive reaches it.** ``diff`` carries redacted values only; the redaction is
structural and happens in :mod:`app.services.audit` before a value is ever put into a
snapshot, so there is no path from a credential to this column to close later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

#: Who performed the change.
#:
#: ``system`` exists because background jobs mutate configuration too — task 09's
#: connector deletion marks documents gone, task 17's reindex rewrites every vector — and
#: an event with no actor at all reads like a gap in the log rather than like a job.
#: ``superadmin_impersonation`` is separated from ``user`` rather than being derivable
#: from the actor's role, because what a customer wants to find in their own log is
#: "somebody from the vendor was in here", and that has to be a filter, not an inference.
ACTOR_TYPES = ("user", "system", "superadmin_impersonation")

#: What was changed. Free-form would be a mistake: the contextual "Audit" tabs filter on
#: this pair, so a typo in a new hook would silently produce an event nobody ever sees.
TARGET_TYPES = (
    "organization",
    "user",
    "invitation",
    "upstream_model",
    "connector",
    "document",
    "gateway",
    "api_key",
    "end_user",
    "memory_fact",
    "platform_settings",
)

MAX_ACTION_LENGTH = 64
MAX_LABEL_LENGTH = 200


class AuditEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user', 'system', 'superadmin_impersonation')",
            name="actor_type_is_known",
        ),
        # The three reads this table serves. `organization_id` leads on the first, as it
        # does on every composite index here, so the scoped list is an index scan.
        Index("ix_audit_events_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_audit_events_actor_user_id_created_at", "actor_user_id", "created_at"),
        # The contextual tab: "everything that ever happened to this gateway".
        Index("ix_audit_events_target_type_target_id", "target_type", "target_id"),
        # Cursor pagination orders by `id`, not by `created_at` — UUIDv7 sorts by
        # creation time, so the cursor is one column and needs no tiebreak. The index
        # above answers "which events, in this window"; this one answers "the next fifty".
        Index("ix_audit_events_organization_id_id", "organization_id", "id"),
    )

    #: ``NULL`` for a platform-level event — creating a global model, changing platform
    #: settings — which belongs to no customer and appears in no customer's log. It is
    #: the tenant key the scope clause filters on, and nothing more: no foreign key, for
    #: the reason in the module docstring.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: The actor's email as it was, so a removed member's actions still have a name.
    actor_label: Mapped[str | None] = mapped_column(String(MAX_LABEL_LENGTH), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user")

    action: Mapped[str] = mapped_column(String(MAX_ACTION_LENGTH), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``NULL`` for a target with no row of its own — platform settings, and the bulk
    #: operations that summarise many rows into one event.
    target_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    target_label: Mapped[str | None] = mapped_column(String(MAX_LABEL_LENGTH), nullable=True)

    #: ``{"changes": [{"path", "before", "after"}, ...]}``, plus ``summary`` for a bulk
    #: operation. A missing ``before`` means the field did not exist — a creation — and a
    #: missing ``after`` means it no longer does. See :mod:`app.services.audit`.
    diff: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # INET rather than text, for the same reason `sessions.ip` is: an investigation that
    # cannot ask "what else came from this subnet" is much less useful.
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Ties an event to the access log line and, for a data-plane-triggered job, to the
    #: request that started it.
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
