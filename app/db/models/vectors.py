"""Which vector backend an organization is on, and which collection is live there.

One row per organization, and the primary key *is* the organization id — there is no
surrogate, because "this tenant is on two backends" is not a state this system has and a
table that can represent it is a table that will eventually contain it.

**Why a table rather than a key in ``organizations.settings``.** That JSONB column holds
org-level *defaults* — a logging policy, a distillation model — values a screen may
override and nothing breaks if it is absent. This is neither. It is read on the retrieval
path of every request that uses memory, it decides which server a query is sent to, and an
unconstrained value in it means an organization whose vectors are addressed to a backend
this deployment does not have. So it is columns, with a check constraint.

**Why ``collection`` is here at all.** Qdrant does not need it: an alias is a pointer
stored beside the data and moving it is one atomic operation. Chroma has no equivalent, so
the pointer has to live somewhere this deployment controls, and the alternatives are worse
in ways :class:`~app.services.vector_index.LiveCollections` sets out. The column is
therefore **nullable and backend-specific**: for a Qdrant-bound organization it stays null
and the alias remains authoritative, because two records of the same fact is how they come
to disagree. A test asserts that.

**``status`` is what makes a migration visible.** ``bound`` is the ordinary state.
``migrating`` says a copy into another backend is in flight — reads still go to
``backend``, which is what keeps retrieval working throughout — and it exists so a second
migration for the same organization is refused by a constraint somebody cannot forget to
check rather than by a query somebody might.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: The backends this build can talk to. A check constraint rather than an enum type:
#: adding a backend should be a migration that widens a string, not one that rewrites a
#: type every table referencing it depends on.
VECTOR_BACKENDS = ("qdrant", "chroma")

#: ``migrating`` is "reads go to ``backend``, a copy is being built in ``target``".
VECTOR_BINDING_STATUSES = ("bound", "migrating")


class VectorBinding(Base):
    __tablename__ = "vector_bindings"
    __table_args__ = (
        CheckConstraint("backend IN ('qdrant', 'chroma')", name="backend_is_known"),
        CheckConstraint("target IS NULL OR target IN ('qdrant', 'chroma')", name="target_is_known"),
        CheckConstraint("status IN ('bound', 'migrating')", name="status_is_known"),
        # The two halves of "migrating" cannot come apart. A row claiming a migration with
        # no destination, or a destination with no migration, is a state every reader
        # would have to defend against — so it is refused here instead.
        CheckConstraint(
            "(status = 'migrating') = (target IS NOT NULL)", name="migrating_has_a_target"
        ),
        CheckConstraint("target IS NULL OR target <> backend", name="target_is_elsewhere"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    backend: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The live collection, for a backend that cannot answer that about itself. Null for
    #: Qdrant, whose alias is authoritative. See the module docstring.
    collection: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="bound")
    #: Where a migration in flight is copying to. Null unless ``status = 'migrating'``.
    target: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The live collection *in the target backend* while a migration is building it. Kept
    #: separate from ``collection`` so a promotion is one write that swaps both, and so a
    #: migration that is abandoned leaves nothing pointing into the backend it was
    #: leaving for.
    target_collection: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
