"""One row per distillation pass (SPEC §6.4, §10.1).

Task 13 turns transcripts into facts on a background worker, and this table is the record
of that happening. It exists for three jobs that a Prometheus counter cannot do.

**The health signals in SPEC §10.1 are rates over a window, and a window outlives a
process.** "Facts written per day", "distillation failure rate", "dedupe rate",
"supersession rate" are questions asked over the last thirty days from a browser, by an
operator who has restarted the workers twice in that time. Counters answer them for as
long as a process lives; a row answers them for as long as the row is kept. Both exist —
the counters are what an alert fires on, and these rows are what the screen charts.

**A dedupe rate near 100% and a supersession rate near zero are the two failure modes
that look like success.** Extraction that produces nothing new, and contradiction that is
never caught, both present as a healthy-looking job that succeeds every time. Neither is
visible without counting what each pass *did* rather than whether it finished, which is
why the five dispositions are five columns and not one.

**The daily cap is counted from here.** A cost guard has to be read before the call it
guards, and reading it from the same table the charts read means the number the Settings
screen shows is the number the guard actually used — rather than a Redis counter that
agrees with it most of the time and diverges silently after an eviction.

There are no foreign keys to ``upstream_models``: the model that distilled a conversation
last month may have been deleted since, and the run is still a true record of what
happened. ``model_name`` is denormalised for the same reason ``request_logs`` denormalises
it.

The table grows at one row per active conversation per debounce window, which is orders of
magnitude below ``request_logs`` — a burst of forty turns is one row. It is not
partitioned for that reason; task 17's retention job prunes it by age like everything else.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

#: What a pass ended as. ``skipped`` is recorded only when the *reason is worth seeing* —
#: the daily cap bit, or the org switched distillation off mid-flight — never for the
#: ordinary "nothing new to read", which would be a row per no-op job and would drown the
#: rates in noise.
RUN_OUTCOMES = ("succeeded", "failed", "skipped")

#: Longest failure reason stored. A provider's error page is not a reason.
MAX_REASON_LENGTH = 500


class DistillationRun(Base, UUIDPrimaryKeyMixin):
    """One attempt to turn a conversation into durable facts."""

    __tablename__ = "distillation_runs"
    __table_args__ = (
        CheckConstraint("outcome IN ('succeeded', 'failed', 'skipped')", name="outcome_is_known"),
        CheckConstraint(
            "candidates >= 0 AND inserted >= 0 AND deduped >= 0 AND superseded >= 0 "
            "AND evicted >= 0 AND rejected >= 0",
            name="counts_are_not_negative",
        ),
        # Every read of this table is "this organization, this window": the charts, the
        # daily cap, and the retention prune.
        Index("ix_distillation_runs_organization_id_created_at", "organization_id", "created_at"),
        # The per-user frequency cap, which is the same question one person at a time.
        Index("ix_distillation_runs_end_user_id_created_at", "end_user_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Whose conversation this was. Cascades: a deleted end user's runs are about a person
    #: who no longer exists, and unlike a request log they carry no traffic figures that
    #: a chart would silently lose.
    end_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The thread, as resolved by :func:`app.services.end_user.session_key`. Null when a
    #: backfill distilled a transcript that carried no session.
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Why it failed, or why it was skipped. Null on success.
    reason: Mapped[str | None] = mapped_column(String(MAX_REASON_LENGTH), nullable=True)

    #: Which model did the extracting. No foreign key; see the module docstring.
    model_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: How many transcripts this pass read. The debounce is working when this is > 1 for
    #: an active conversation.
    transcripts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: What the model proposed, after validation dropped whatever was malformed.
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The five dispositions. They sum to ``candidates`` except for ``evicted``, which is
    #: about the facts already stored rather than about this pass's output.
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    superseded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evicted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Proposed and refused: malformed, out of range, an unknown kind, a ``supersedes``
    #: pointing at somebody else's fact, or a sentence that reads as an instruction to
    #: the assistant rather than as a fact about the person.
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["MAX_REASON_LENGTH", "RUN_OUTCOMES", "DistillationRun"]
