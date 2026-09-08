"""Platform-level state: the settings an operator can change, and the jobs that run.

Three tables, and the reason they are here rather than spread across the modules that
use them is that all three are read by one screen — Platform — and none of them belongs
to a tenant.

**``platform_settings`` is a key/value table on purpose.** The alternative, a single-row
table with a column per setting, needs a migration for every knob and gives no place to
record *who* changed one. Here a setting is a row, the row carries its own attribution,
and :mod:`app.services.platform_settings` is the schema — which is where a setting's
type, bounds and environment-variable bootstrap already have to live anyway.

**``maintenance_runs`` is one table for three jobs.** Partition management, retention and
the orphan sweep are separate passes with separate schedules, but the question asked of
all three is identical: when did it last run, what did it touch, and did it finish. A
table each would be three shapes for one question and three queries on one screen.

**A reindex is a run plus a row per organization.** The run is the operator's decision —
these models, this scope, this cost; the target rows are the work, and each one carries
its own cursor so a reindex killed halfway resumes per organization rather than starting
the whole platform again. Splitting them is also what keeps ``organization_id`` off the
run: a platform-wide reindex belongs to nobody, and a nullable tenant column on the row
an operator reads is the kind of thing that later gets filtered on by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin

#: The jobs :mod:`app.services.maintenance` runs on a schedule.
MAINTENANCE_JOBS = ("partitions", "retention", "sweep")

#: ``running`` exists so a resumable job can find its own unfinished attempt. A run that
#: was killed stays ``running`` until the next pass adopts it — which is exactly what
#: makes resumption possible, and why there is no separate "abandoned" state to reconcile.
RUN_STATUSES = ("running", "succeeded", "failed")

#: What a reindex covers. ``platform`` is every organization; ``organization`` is one.
#: There is no connector scope: an embedding model is a property of a whole collection,
#: so re-embedding part of one would leave a tenant's index holding two models' vectors.
REINDEX_SCOPES = ("platform", "organization")

#: A target's own progression. ``verifying`` is between the last upsert and the alias
#: swap, and it is a state rather than a moment because the count check and the sample
#: search can both fail — leaving the old collection serving, which is the whole point.
TARGET_STATUSES = ("pending", "embedding", "verifying", "swapped", "failed")


class PlatformSetting(Base):
    """One operator-configured value (SPEC §15.3, task 17).

    ``value`` is JSONB rather than text because half of these are objects — the logging
    defaults, the rate-limit quotas — and storing those as encoded strings would mean the
    database could not be read without the application.
    """

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    #: Nullable because the bootstrap writes none of these — a value that came from an
    #: environment variable has no author, and inventing one would be a lie in the column
    #: an auditor reads. No foreign key for the same reason request logs have none: the
    #: record has to survive the user being deleted.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MaintenanceRun(Base, UUIDPrimaryKeyMixin):
    """One pass of one scheduled job.

    ``cursor`` is what makes a pass resumable: retention writes the last
    ``(gateway, day)`` it finished into it, and a run adopted after a crash picks up from
    there instead of re-scanning days it has already cleared. It is deliberately an
    *optimisation* rather than the correctness mechanism — every unit of work here is
    idempotent, so a lost cursor costs time and nothing else.

    ``report`` is the operator-facing half: rows and bytes per gateway, partitions created
    and dropped, orphans found. Kept as JSONB because its shape differs per job and
    normalising it would be three more tables nobody queries by column.
    """

    __tablename__ = "maintenance_runs"
    __table_args__ = (
        CheckConstraint("job IN ('partitions', 'retention', 'sweep')", name="job_is_known"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status_is_known"),
        # The Maintenance screen asks for the newest run of each job, and the resumption
        # check asks for the newest *running* one. Both are this index.
        Index("ix_maintenance_runs_job_started_at", "job", "started_at"),
    )

    job: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReindexRun(Base, UUIDPrimaryKeyMixin):
    """An embedding-model migration, as an operator decided it (SPEC §9.4).

    The *from* model is recorded as well as the *to*, because the question asked six
    months later is "what changed when retrieval got worse", and a row that only says
    where it was going cannot answer it.
    """

    __tablename__ = "reindex_runs"
    __table_args__ = (
        CheckConstraint("scope IN ('platform', 'organization')", name="scope_is_known"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status_is_known"),
        CheckConstraint("to_dimension > 0", name="dimension_is_positive"),
        Index("ix_reindex_runs_status_started_at", "status", "started_at"),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="platform")
    #: Set for an ``organization`` reindex, null for a platform one. Not called
    #: ``organization_id``: this table is the operator's, and a tenant-shaped column here
    #: would put a platform-wide run — whose value is null — inside the scope guard's remit
    #: for no benefit. The per-tenant rows are in ``reindex_targets``.
    scope_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    from_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_model: Mapped[str] = mapped_column(Text, nullable=False)
    to_dimension: Mapped[int] = mapped_column(Integer, nullable=False)

    #: What the operator was shown before they confirmed. Stored so the bill can be
    #: compared against the estimate that justified it.
    estimated_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Who pressed the button. Null for a run started by a job rather than a person.
    started_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReindexTarget(Base, UUIDPrimaryKeyMixin):
    """One organization's share of a reindex, and the cursor that resumes it.

    ``collection`` is the *physical* name being built — ``org_{id}_docs_v3`` — not the
    alias. Recorded because a run that failed between "created" and "swapped" leaves a
    collection behind, and an operator cleaning up should not have to guess its version.
    """

    __tablename__ = "reindex_targets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'embedding', 'verifying', 'swapped', 'failed')",
            name="status_is_known",
        ),
        CheckConstraint("done_points >= 0", name="done_is_not_negative"),
        UniqueConstraint("run_id", "organization_id", name="uq_reindex_targets_run_organization"),
        Index("ix_reindex_targets_run_id_id", "run_id", "id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("reindex_runs.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    total_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Qdrant's scroll offset, as returned by the last page that was copied. A string
    #: because a point id is one; null means "start at the beginning".
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "MAINTENANCE_JOBS",
    "REINDEX_SCOPES",
    "RUN_STATUSES",
    "TARGET_STATUSES",
    "MaintenanceRun",
    "PlatformSetting",
    "ReindexRun",
    "ReindexTarget",
]
