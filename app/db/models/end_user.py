"""End users, and the durable facts the gateway knows about them (SPEC §6.1 B, §6.2).

An **end user** is not a user of this product. It is somebody on the far side of a
customer's application — the person typing into their support widget — and this table
exists so that "what does the assistant remember about them" is a question with an
answer, a screen, and a delete button.

Three consequences run through both tables here.

**The identity is a string a customer chose, and we do not trust it.** ``external_id``
arrives in a header or in the OpenAI ``user`` field, which means it arrives from whatever
the customer's backend puts there, which in a badly built integration means from the
browser. It is length-capped in the column as well as in the service, unique only *within*
an organization, and never rendered anywhere a model could read it as an instruction. Two
organizations both calling their end user ``alice`` is the ordinary case, not a collision.

**A fact is history, and history is edited by superseding, not by overwriting.**
``superseded_at`` is what task 13's distillation sets when a newer fact contradicts an
older one — "works in Berlin" after "works in Munich" — and the old row stays, because the
memory browser's job is to explain why the assistant said something last week. Recall
filters superseded and expired rows out; nothing deletes them but a purge.

**``source_log_id`` has no foreign key.** It points at ``request_logs``, which is
partitioned by day and dropped by retention: a fact distilled from a conversation must
outlive the transcript it came from, and an ``ON DELETE CASCADE`` here would quietly erase
the memory when the log window rolled over. The column is a breadcrumb — the drawer looks
the row up and says "(no longer retained)" when it has gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: SPEC §6.4's structured-output vocabulary. A CHECK rather than an enum, for the same
#: reason every other status column in this schema is one: adding ``skill`` in a later
#: task is a constraint swap inside a transaction, and ``ALTER TYPE`` is not.
FACT_KINDS = ("preference", "fact", "goal", "constraint")

#: Longest ``external_id`` accepted. Long enough for a UUID, an email, or an opaque
#: provider subject; short enough that a caller cannot make the unique index's key
#: arbitrarily large, and short enough to be a b-tree key rather than a TOAST pointer.
MAX_EXTERNAL_ID_LENGTH = 200

#: Longest fact text stored. A fact is a sentence — "prefers Python", "works in the EU and
#: needs GDPR-compliant answers". Anything past this length is a summary of a conversation
#: rather than a durable fact about a person, and injecting it would spend the whole
#: memory budget on one entry.
MAX_FACT_LENGTH = 1000


class EndUser(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One identity on the far side of a customer's application.

    Created on first sight, which happens on the request path — see
    :class:`app.services.end_user_store.EndUserStore`. The counters below are *not*
    written on the request path: ``request_count`` and ``last_seen_at`` are batched, so a
    busy end user costs one write per flush interval rather than one per request.
    """

    __tablename__ = "end_users"
    __table_args__ = (
        CheckConstraint("request_count >= 0", name="request_count_is_not_negative"),
        # The identity lookup, and the thing that makes "alice" mean two different people
        # in two organizations. Also the conflict target of the upsert that creates rows.
        UniqueConstraint(
            "organization_id", "external_id", name="uq_end_users_organization_id_external_id"
        ),
        # The list screen: one organization's end users, newest first.
        Index("ix_end_users_organization_id_id", "organization_id", "id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: What the customer's system calls this person. Untrusted input; see the module
    #: docstring.
    external_id: Mapped[str] = mapped_column(String(MAX_EXTERNAL_ID_LENGTH), nullable=False)
    #: An optional human name, set from the control plane only. Never populated from a
    #: request header — a display name that a caller can set is a display name an attacker
    #: can set, and this one appears in an operator's browser.
    label: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    facts: Mapped[list[MemoryFact]] = relationship(
        back_populates="end_user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MemoryFact(Base, UUIDPrimaryKeyMixin):
    """One durable thing the gateway knows about one end user (SPEC §6.4).

    No :class:`~app.db.base.TimestampMixin`: ``created_at`` is here but ``updated_at`` is
    not, because the two timestamps that matter for a fact are when it was first learned
    and when it was last *observed* — and ``last_seen_at`` is bumped by re-observation,
    not by an edit. A generic ``updated_at`` beside them would be a third date that means
    neither, and the recency decay would eventually be computed from the wrong one.
    """

    __tablename__ = "memory_facts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('preference', 'fact', 'goal', 'constraint')", name="kind_is_known"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_is_a_fraction"),
        CheckConstraint("length(text) > 0", name="text_is_not_empty"),
        CheckConstraint(f"length(text) <= {MAX_FACT_LENGTH}", name="text_is_not_too_long"),
        # Recall reads one end user's facts and the browser lists them; both want this
        # order. `id` is a UUIDv7, so descending id is descending creation time.
        Index("ix_memory_facts_end_user_id_id", "end_user_id", "id"),
        Index("ix_memory_facts_organization_id_id", "organization_id", "id"),
        # Partial, because recall never reads a superseded fact and superseded rows
        # accumulate forever. The index stays the size of the *live* memory rather than
        # of everything the assistant has ever believed.
        Index(
            "ix_memory_facts_live",
            "end_user_id",
            "last_seen_at",
            postgresql_where=sql_text("superseded_at IS NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    end_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
    )

    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="fact")
    #: How sure the writer is. Manual entry is 1.0 — a person typed it — and distillation
    #: (task 13) supplies the model's own number. It is a *ranking* input, not a filter:
    #: a low-confidence fact still surfaces when nothing better matches.
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: The request this was learned from, if any. No foreign key; see the module docstring.
    source_log_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    #: Set instead of deleting when a newer fact replaces this one. Recall skips it; the
    #: browser shows it greyed, because "why did it say that last week" needs the row.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The fact that replaced this one, when there is one. Task 13's distillation sets it
    #: alongside ``superseded_at``; a retraction by hand sets only the timestamp, because
    #: nothing replaced it.
    #:
    #: ``ON DELETE SET NULL`` rather than ``CASCADE``: deleting the replacement must not
    #: delete the history it replaced. The row then reads as "retracted, and what replaced
    #: it is gone too", which is the truth.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("memory_facts.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: A fact with a shelf life — "is travelling until the 14th". Null is the common case
    #: and means "until something supersedes it".
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Bumped when the same fact is observed again. This, not ``created_at``, is what the
    #: recency decay reads: a preference restated last week is current, however long ago
    #: it was first learned.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    end_user: Mapped[EndUser] = relationship(back_populates="facts")


__all__ = ["FACT_KINDS", "MAX_EXTERNAL_ID_LENGTH", "MAX_FACT_LENGTH", "EndUser", "MemoryFact"]
