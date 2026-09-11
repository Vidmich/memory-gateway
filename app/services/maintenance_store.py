"""The SQL behind partition management, retention and the orphan sweep.

Kept apart from :mod:`app.services.maintenance`, which holds the *decisions* — how much
runway to keep, which day belongs to which gateway, what to delete and what to leave — so
that those can be tested exhaustively without a database and this can be tested against a
real PostgreSQL with the same handful of assertions.

Three things here are load-bearing.

**Partitions are read from the catalog, not from a list we keep.** ``pg_inherits`` is the
truth about which days exist; a table of our own would drift the first time somebody added
a partition by hand during an incident, and the failure mode of drift is inserts failing
at midnight.

**A partition is removed with ``DROP TABLE``, not ``DELETE``.** That is the whole reason
``request_logs`` is partitioned. Dropping takes a brief ``ACCESS EXCLUSIVE`` lock on the
parent — milliseconds for one child — and returns the disk immediately.
``DETACH CONCURRENTLY`` would avoid even that, and it is deliberately not used: it cannot
run inside a transaction block, and the retention pass needs its other statements to be
transactional. If that lock ever shows up in a latency graph, this is the line to change.

**Pruning is scoped by ``(gateway, day)``.** Not by "everything older than X": retention is
per gateway and partitions are global, so a statement that spanned both would either be
wrong for one gateway or unbounded in size. One day of one gateway is a bounded unit of
work, an index range on ``ix_request_logs_gateway_id_created_at``, and — because it deletes
by predicate rather than by a list of ids — idempotent, which is what makes the whole pass
resumable.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import (
    ApiKey,
    Connector,
    DistillationRun,
    Document,
    EndUser,
    EvaluationItem,
    EvaluationRun,
    EvaluationSet,
    Gateway,
    GatewayTarget,
    IndexAudit,
    Invitation,
    MaintenanceRun,
    MemoryFact,
    Organization,
    ReprocessingRun,
    RequestLog,
    SummarizationRun,
    Transcript,
    UpstreamModel,
    User,
)
from app.db.scoping import unscoped
from app.services.audit import AuditingTransaction, MemoryAuditRecorder, PostgresAuditRecorder
from app.services.memory_db import MemoryDatabase

logger = logging.getLogger(__name__)

#: The two tables task 07 partitions by day. Retention keeps both in step deliberately —
#: a transcript whose metadata row has been dropped is unreachable and undeletable.
PARTITIONED_TABLES = ("request_logs", "transcripts")

_SUFFIX = re.compile(r"_(\d{8})$")

#: Why every statement here spans organizations. Retention, partition management and the
#: sweep are platform jobs by definition: they run for every tenant at once and have no
#: request, no session and no actor to derive a scope from.
_REASON = "scheduled platform maintenance runs across every organization"


@dataclass(frozen=True, slots=True)
class GatewayRetention:
    """One gateway's configured windows, before the platform ceilings are applied."""

    gateway_id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    body_days: int
    metadata_days: int


@dataclass(frozen=True, slots=True)
class Pruned:
    """What one ``(gateway, day)`` unit removed."""

    rows: int = 0
    bytes: int = 0

    def __add__(self, other: Pruned) -> Pruned:
        return Pruned(rows=self.rows + other.rows, bytes=self.bytes + other.bytes)


@dataclass(frozen=True, slots=True)
class ExpiredFact:
    fact_id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OrganizationRow:
    """The tenant lifecycle columns, and nothing else.

    A narrow row rather than the mapped object: this store is the platform's, and handing
    a whole ``Organization`` to a job that only needs its slug and its purge date is how a
    job ends up quietly reading a tenant's settings.
    """

    id: uuid.UUID
    name: str
    slug: str
    status: str
    purge_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConnectorChunking:
    """One connector's chunking settings and what it has indexed, across tenants.

    Read by :class:`~app.services.reindex.Reindexer` to answer the one question a platform
    embedding-model change now has to ask before it starts: which connectors have to be
    *recut* rather than re-embedded. The blob is returned raw and interpreted by the
    reindexer, because deciding what "model-dependent" means is a chunking question and
    this module has no business holding an opinion about it.
    """

    organization_id: uuid.UUID
    connector_id: uuid.UUID
    chunking: dict[str, Any]
    #: Documents currently in a terminal indexed state. What the recut would actually cost.
    indexed_documents: int


@dataclass(frozen=True, slots=True)
class RunState:
    """A maintenance run as the job sees it."""

    id: uuid.UUID
    job: str
    status: str
    started_at: datetime
    cursor: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    finished_at: datetime | None = None
    error: str | None = None


class MaintenanceTransaction(AuditingTransaction, Protocol):
    # -- partitions ------------------------------------------------------
    async def partition_days(self, table: str) -> list[date]: ...

    async def create_partition(self, table: str, day: date) -> None: ...

    async def drop_partition(self, table: str, day: date) -> None: ...

    # -- retention -------------------------------------------------------
    async def retentions(self) -> list[GatewayRetention]: ...

    async def prune_bodies(self, gateway_id: uuid.UUID, day: date) -> Pruned:
        """Delete the transcripts of one gateway's requests on one day.

        The row goes rather than its columns being nulled. SPEC §10.2 says bodies are
        *hard-deleted*, and a row of nulls is a row somebody has to remember means
        "erased" rather than "never captured" — a distinction ``bodies_omitted`` already
        carries on the metadata row, correctly, for the case where nothing was stored.
        """
        ...

    async def prune_metadata(self, gateway_id: uuid.UUID, day: date) -> Pruned:
        """Delete one gateway's metadata rows for one day, transcripts included.

        Both, always: a metadata row is what makes a transcript reachable, so deleting one
        without the other would leave bodies nothing can read and nothing can remove.
        """
        ...

    async def expired_facts(self, *, now: datetime, limit: int) -> list[ExpiredFact]: ...

    async def delete_facts(self, fact_ids: Sequence[uuid.UUID]) -> int: ...

    # -- sweep -----------------------------------------------------------
    async def organizations(self) -> list[tuple[uuid.UUID, str]]: ...

    async def document_ids(self, organization_id: uuid.UUID) -> set[str]: ...

    async def fact_ids(self, organization_id: uuid.UUID) -> set[str]: ...

    async def document_uris(self, organization_id: uuid.UUID) -> set[str]: ...

    async def storage_prefixes(self, organization_id: uuid.UUID) -> list[str]: ...

    # -- reindex ---------------------------------------------------------
    async def connector_chunkings(self) -> list[ConnectorChunking]:
        """Every connector's chunking blob, platform-wide, with its indexed document count."""
        ...

    async def indexed_documents(self, connector_id: uuid.UUID) -> list[uuid.UUID]:
        """The documents a recut of this connector would have to run again, oldest first.

        Only the indexed ones: a document that failed extraction will fail it again, and a
        pending one has a job coming that will cut it with the current settings anyway.
        """
        ...

    # -- runs ------------------------------------------------------------
    async def unfinished(self, job: str) -> RunState | None: ...

    async def open_run(self, job: str) -> RunState: ...

    async def save_run(
        self,
        run_id: uuid.UUID,
        *,
        cursor: Mapping[str, Any] | None = None,
        report: Mapping[str, Any] | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None: ...

    async def recent_runs(self, *, limit: int = 20) -> list[RunState]: ...

    # -- erasure ---------------------------------------------------------
    async def organization(self, organization_id: uuid.UUID) -> OrganizationRow | None: ...

    async def mark_deleting(self, organization_id: uuid.UUID, *, purge_after: datetime) -> None: ...

    async def clear_deleting(self, organization_id: uuid.UUID) -> None: ...

    async def due_for_purge(self, now: datetime) -> list[uuid.UUID]: ...

    async def purge_organization(self, organization_id: uuid.UUID) -> dict[str, int]:
        """Every row belonging to one organization, counted per table.

        Explicit deletes in dependency order rather than one ``DELETE FROM organizations``
        and a set of cascades. Two reasons, and the second is the real one: the partitioned
        log tables carry no foreign keys at all — deliberately, so a log survives the
        gateway it describes — so a cascade would leave them behind; and a report saying
        which table gave up how many rows is the artefact this exists to produce, which a
        cascade cannot give.
        """
        ...

    async def commit(self) -> None: ...


class MaintenanceStore(Protocol):
    def begin(self) -> AbstractAsyncContextManager[MaintenanceTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


def partition_name(table: str, day: date) -> str:
    return f"{table}_{day.strftime('%Y%m%d')}"


def day_of(partition: str) -> date | None:
    """The day a partition name encodes, or ``None`` for ``_default``."""
    found = _SUFFIX.search(partition)
    if found is None:
        return None
    try:
        return datetime.strptime(found.group(1), "%Y%m%d").date()
    except ValueError:  # pragma: no cover - a name that looks like a date and is not
        return None


def create_partition_sql(table: str, day: date) -> str:
    """Bounds as explicit UTC timestamps, exactly as the task 07 migration writes them.

    A bare date literal is interpreted in the session's ``TimeZone``, so the same DDL run
    by two operators in two places would carve the day differently and rows near midnight
    would land either side of the boundary.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(table, day)} PARTITION OF {table} "
        f"FOR VALUES FROM ('{day.isoformat()} 00:00:00+00') "
        f"TO ('{(day + timedelta(days=1)).isoformat()} 00:00:00+00')"
    )


_CHILDREN = text(
    """
    SELECT child.relname AS name
    FROM pg_inherits
    JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
    JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
    WHERE parent.relname = :table
    """
)


class PostgresMaintenanceTransaction(PostgresAuditRecorder):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- partitions ------------------------------------------------------

    async def partition_days(self, table: str) -> list[date]:
        rows = await self._session.execute(_CHILDREN, {"table": table})
        days = [day_of(str(name)) for (name,) in rows.all()]
        return sorted(day for day in days if day is not None)

    async def create_partition(self, table: str, day: date) -> None:
        await self._session.execute(text(create_partition_sql(table, day)))

    async def drop_partition(self, table: str, day: date) -> None:
        # ``IF EXISTS`` because two replicas running the pass at once is a race worth
        # winning quietly rather than a reason for one of them to fail.
        await self._session.execute(text(f"DROP TABLE IF EXISTS {partition_name(table, day)}"))

    # -- retention -------------------------------------------------------

    async def retentions(self) -> list[GatewayRetention]:
        from app.schemas.gateway_config import LoggingConfig

        rows = await self._session.execute(
            select(
                Gateway.id, Gateway.organization_id, Gateway.name, Gateway.logging_config
            ).execution_options(**unscoped(_REASON))
        )
        found: list[GatewayRetention] = []
        for gateway_id, organization_id, name, stored in rows.all():
            config = LoggingConfig.load(stored)
            found.append(
                GatewayRetention(
                    gateway_id=gateway_id,
                    organization_id=organization_id,
                    name=name,
                    body_days=config.retention_days,
                    metadata_days=config.metadata_retention_days,
                )
            )
        return found

    async def prune_bodies(self, gateway_id: uuid.UUID, day: date) -> Pruned:
        start, end = _bounds(day)
        # Sized before deleting, because ``DELETE`` reports rows and the operator's
        # question is disk. One extra aggregate per (gateway, day) against the same index
        # range the delete uses.
        measured = await self._session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(func.pg_column_size(Transcript.request_body)), 0)
                + func.coalesce(func.sum(func.pg_column_size(Transcript.assembled_prompt)), 0)
                + func.coalesce(func.sum(func.pg_column_size(Transcript.response_body)), 0),
            )
            .where(
                Transcript.created_at >= start,
                Transcript.created_at < end,
                Transcript.request_log_id.in_(_log_ids(gateway_id, start, end)),
            )
            .execution_options(**unscoped(_REASON))
        )
        rows, size = measured.one()
        if not rows:
            return Pruned()
        await self._session.execute(
            delete(Transcript)
            .where(
                Transcript.created_at >= start,
                Transcript.created_at < end,
                Transcript.request_log_id.in_(_log_ids(gateway_id, start, end)),
            )
            .execution_options(**unscoped(_REASON))
        )
        return Pruned(rows=int(rows), bytes=int(size or 0))

    async def prune_metadata(self, gateway_id: uuid.UUID, day: date) -> Pruned:
        start, end = _bounds(day)
        bodies = await self.prune_bodies(gateway_id, day)
        result = await self._session.execute(
            delete(RequestLog)
            .where(
                RequestLog.gateway_id == gateway_id,
                RequestLog.created_at >= start,
                RequestLog.created_at < end,
            )
            .execution_options(**unscoped(_REASON))
        )
        return Pruned(rows=int(getattr(result, "rowcount", 0) or 0), bytes=bodies.bytes)

    async def expired_facts(self, *, now: datetime, limit: int) -> list[ExpiredFact]:
        rows = await self._session.execute(
            select(MemoryFact.id, MemoryFact.organization_id)
            .where(MemoryFact.expires_at.is_not(None), MemoryFact.expires_at < now)
            .order_by(MemoryFact.id)
            .limit(limit)
            .execution_options(**unscoped(_REASON))
        )
        return [
            ExpiredFact(fact_id=fact_id, organization_id=organization_id)
            for fact_id, organization_id in rows.all()
        ]

    async def delete_facts(self, fact_ids: Sequence[uuid.UUID]) -> int:
        if not fact_ids:
            return 0
        result = await self._session.execute(
            delete(MemoryFact)
            .where(MemoryFact.id.in_(list(fact_ids)))
            .execution_options(**unscoped(_REASON))
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # -- sweep -----------------------------------------------------------

    async def organizations(self) -> list[tuple[uuid.UUID, str]]:
        rows = await self._session.execute(
            select(Organization.id, Organization.name)
            .order_by(Organization.id)
            .execution_options(**unscoped(_REASON))
        )
        return [(row[0], row[1]) for row in rows.all()]

    async def document_ids(self, organization_id: uuid.UUID) -> set[str]:
        rows = await self._session.execute(
            select(Document.id)
            .where(Document.organization_id == organization_id)
            .execution_options(**unscoped(_REASON))
        )
        return {str(row[0]) for row in rows.all()}

    async def connector_chunkings(self) -> list[ConnectorChunking]:
        indexed = (
            select(Document.connector_id, func.count().label("indexed"))
            .where(Document.status == "indexed")
            .group_by(Document.connector_id)
            .subquery()
        )
        rows = await self._session.execute(
            select(
                Connector.organization_id,
                Connector.id,
                Connector.chunking,
                func.coalesce(indexed.c.indexed, 0),
            )
            .outerjoin(indexed, indexed.c.connector_id == Connector.id)
            .where(Connector.status != "deleting")
            .order_by(Connector.organization_id, Connector.id)
            .execution_options(**unscoped(_REASON))
        )
        return [
            ConnectorChunking(
                organization_id=row[0],
                connector_id=row[1],
                chunking=dict(row[2] or {}),
                indexed_documents=int(row[3] or 0),
            )
            for row in rows.all()
        ]

    async def indexed_documents(self, connector_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await self._session.execute(
            select(Document.id)
            .where(Document.connector_id == connector_id, Document.status == "indexed")
            .order_by(Document.id)
            .execution_options(**unscoped(_REASON))
        )
        return [row[0] for row in rows.all()]

    async def fact_ids(self, organization_id: uuid.UUID) -> set[str]:
        rows = await self._session.execute(
            select(MemoryFact.id)
            .where(MemoryFact.organization_id == organization_id)
            .execution_options(**unscoped(_REASON))
        )
        return {str(row[0]) for row in rows.all()}

    async def document_uris(self, organization_id: uuid.UUID) -> set[str]:
        rows = await self._session.execute(
            select(Document.source_uri)
            .where(Document.organization_id == organization_id)
            .execution_options(**unscoped(_REASON))
        )
        return {str(row[0]) for row in rows.all()}

    async def storage_prefixes(self, organization_id: uuid.UUID) -> list[str]:
        rows = await self._session.execute(
            select(Connector.storage_prefix)
            .where(
                Connector.organization_id == organization_id,
                Connector.storage_prefix.is_not(None),
            )
            .execution_options(**unscoped(_REASON))
        )
        return [str(row[0]) for row in rows.all()]

    # -- runs ------------------------------------------------------------

    async def unfinished(self, job: str) -> RunState | None:
        rows = await self._session.execute(
            select(MaintenanceRun)
            .where(MaintenanceRun.job == job, MaintenanceRun.status == "running")
            .order_by(MaintenanceRun.started_at.desc())
            .limit(1)
            .execution_options(**unscoped(_REASON))
        )
        row = rows.scalars().first()
        return _state_of(row) if row is not None else None

    async def open_run(self, job: str) -> RunState:
        run = MaintenanceRun(id=uuid7(), job=job, status="running", started_at=datetime.now(UTC))
        self._session.add(run)
        await self._session.flush()
        return _state_of(run)

    async def save_run(
        self,
        run_id: uuid.UUID,
        *,
        cursor: Mapping[str, Any] | None = None,
        report: Mapping[str, Any] | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if cursor is not None:
            values["cursor"] = dict(cursor)
        if report is not None:
            values["report"] = dict(report)
        if status is not None:
            values["status"] = status
            if status != "running":
                values["finished_at"] = datetime.now(UTC)
        if error is not None:
            values["error"] = error
        if not values:
            return
        await self._session.execute(
            update(MaintenanceRun)
            .where(MaintenanceRun.id == run_id)
            .values(**values)
            .execution_options(**unscoped(_REASON))
        )

    async def recent_runs(self, *, limit: int = 20) -> list[RunState]:
        rows = await self._session.execute(
            select(MaintenanceRun)
            .order_by(MaintenanceRun.started_at.desc())
            .limit(limit)
            .execution_options(**unscoped(_REASON))
        )
        return [_state_of(row) for row in rows.scalars().all()]

    # -- erasure ---------------------------------------------------------

    async def organization(self, organization_id: uuid.UUID) -> OrganizationRow | None:
        rows = await self._session.execute(
            select(
                Organization.id,
                Organization.name,
                Organization.slug,
                Organization.status,
                Organization.purge_after,
            )
            .where(Organization.id == organization_id)
            .execution_options(**unscoped(_REASON))
        )
        row = rows.first()
        return OrganizationRow(*row) if row is not None else None

    async def mark_deleting(self, organization_id: uuid.UUID, *, purge_after: datetime) -> None:
        await self._session.execute(
            update(Organization)
            .where(Organization.id == organization_id)
            .values(status="deleting", purge_after=purge_after)
            .execution_options(**unscoped(_REASON))
        )

    async def clear_deleting(self, organization_id: uuid.UUID) -> None:
        await self._session.execute(
            update(Organization)
            .where(Organization.id == organization_id)
            .values(status="active", purge_after=None)
            .execution_options(**unscoped(_REASON))
        )

    async def due_for_purge(self, now: datetime) -> list[uuid.UUID]:
        rows = await self._session.execute(
            select(Organization.id)
            .where(
                Organization.status == "deleting",
                Organization.purge_after.is_not(None),
                Organization.purge_after <= now,
            )
            .order_by(Organization.id)
            .execution_options(**unscoped(_REASON))
        )
        return [row[0] for row in rows.all()]

    async def purge_organization(self, organization_id: uuid.UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label, model in PURGE_ORDER:
            result = await self._session.execute(
                delete(model)
                .where(model.organization_id == organization_id)
                .execution_options(**unscoped(_REASON))
            )
            counts[label] = int(getattr(result, "rowcount", 0) or 0)
        result = await self._session.execute(
            delete(Organization)
            .where(Organization.id == organization_id)
            .execution_options(**unscoped(_REASON))
        )
        counts["organizations"] = int(getattr(result, "rowcount", 0) or 0)
        return counts

    async def commit(self) -> None:
        await self._session.commit()


#: Deletion order. Children before parents, and the two unparented log tables first so
#: that a pass which fails partway has already removed the customer content rather than
#: the configuration describing it. ``audit_events`` is deliberately absent — see
#: :mod:`app.services.erasure`.
PURGE_ORDER: tuple[tuple[str, Any], ...] = (
    ("transcripts", Transcript),
    ("request_logs", RequestLog),
    ("distillation_runs", DistillationRun),
    ("summarization_runs", SummarizationRun),
    ("evaluation_runs", EvaluationRun),
    ("evaluation_items", EvaluationItem),
    ("evaluation_sets", EvaluationSet),
    ("index_audits", IndexAudit),
    ("reprocessing_runs", ReprocessingRun),
    ("memory_facts", MemoryFact),
    ("end_users", EndUser),
    ("documents", Document),
    ("connectors", Connector),
    ("api_keys", ApiKey),
    ("gateway_targets", GatewayTarget),
    ("gateways", Gateway),
    ("upstream_models", UpstreamModel),
    ("invitations", Invitation),
    ("users", User),
)


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _log_ids(gateway_id: uuid.UUID, start: datetime, end: datetime) -> Any:
    """The ids of one gateway's requests in one day, as a subquery.

    A subquery rather than a join in the ``DELETE`` because both tables are partitioned on
    the same column and the bounds are repeated on the outer statement — which is what
    lets PostgreSQL prune to a single partition on each side instead of planning across
    every day that exists.
    """
    return (
        select(RequestLog.id)
        .where(
            RequestLog.gateway_id == gateway_id,
            RequestLog.created_at >= start,
            RequestLog.created_at < end,
        )
        .scalar_subquery()
    )


def _state_of(run: MaintenanceRun) -> RunState:
    return RunState(
        id=run.id,
        job=run.job,
        status=run.status,
        started_at=run.started_at,
        cursor=dict(run.cursor or {}),
        report=dict(run.report or {}),
        finished_at=run.finished_at,
        error=run.error,
    )


class PostgresMaintenanceStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[MaintenanceTransaction]:
        async with self._session_factory() as session:
            yield PostgresMaintenanceTransaction(session)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryMaintenanceTransaction(MemoryAuditRecorder):
    """The same shape over :class:`~app.services.memory_db.MemoryDatabase`.

    Partitions are modelled as a set of days per table. That is not a pretence about
    PostgreSQL — it is the *only* thing the policy layer asks of them, so a double that
    tracked anything more would be inventing behaviour for the tests to depend on.
    """

    _db: MemoryDatabase
    partitions: dict[str, set[date]]

    async def partition_days(self, table: str) -> list[date]:
        return sorted(self.partitions.get(table, set()))

    async def create_partition(self, table: str, day: date) -> None:
        self.partitions.setdefault(table, set()).add(day)

    async def drop_partition(self, table: str, day: date) -> None:
        self.partitions.setdefault(table, set()).discard(day)
        for identifier, row in list(self._db.request_logs.items()):
            if row.created_at.date() == day:
                del self._db.request_logs[identifier]
                self._db.transcripts.pop(identifier, None)
        for identifier, body in list(self._db.transcripts.items()):
            if body.created_at.date() == day:
                del self._db.transcripts[identifier]

    async def retentions(self) -> list[GatewayRetention]:
        from app.schemas.gateway_config import LoggingConfig

        found = []
        for gateway in self._db.gateways.values():
            config = LoggingConfig.load(gateway.logging_config)
            found.append(
                GatewayRetention(
                    gateway_id=gateway.id,
                    organization_id=gateway.organization_id,
                    name=gateway.name,
                    body_days=config.retention_days,
                    metadata_days=config.metadata_retention_days,
                )
            )
        return found

    async def prune_bodies(self, gateway_id: uuid.UUID, day: date) -> Pruned:
        pruned = Pruned()
        for identifier in self._logs_of(gateway_id, day):
            body = self._db.transcripts.pop(identifier, None)
            if body is not None:
                pruned += Pruned(rows=1, bytes=_size_of(body))
        return pruned

    async def prune_metadata(self, gateway_id: uuid.UUID, day: date) -> Pruned:
        bodies = await self.prune_bodies(gateway_id, day)
        rows = 0
        for identifier in self._logs_of(gateway_id, day):
            del self._db.request_logs[identifier]
            rows += 1
        return Pruned(rows=rows, bytes=bodies.bytes)

    def _logs_of(self, gateway_id: uuid.UUID, day: date) -> list[uuid.UUID]:
        return [
            identifier
            for identifier, row in self._db.request_logs.items()
            if row.gateway_id == gateway_id and row.created_at.date() == day
        ]

    async def expired_facts(self, *, now: datetime, limit: int) -> list[ExpiredFact]:
        found = [
            ExpiredFact(fact_id=fact.id, organization_id=fact.organization_id)
            for fact in self._db.memory_facts.values()
            if fact.expires_at is not None and fact.expires_at < now
        ]
        found.sort(key=lambda entry: entry.fact_id)
        return found[:limit]

    async def delete_facts(self, fact_ids: Sequence[uuid.UUID]) -> int:
        removed = 0
        for fact_id in fact_ids:
            if self._db.memory_facts.pop(fact_id, None) is not None:
                removed += 1
        return removed

    async def organizations(self) -> list[tuple[uuid.UUID, str]]:
        return sorted(
            ((row.id, row.name) for row in self._db.organizations.values()),
            key=lambda entry: entry[0],
        )

    async def document_ids(self, organization_id: uuid.UUID) -> set[str]:
        return {
            str(row.id)
            for row in self._db.documents.values()
            if row.organization_id == organization_id
        }

    async def connector_chunkings(self) -> list[ConnectorChunking]:
        return [
            ConnectorChunking(
                organization_id=row.organization_id,
                connector_id=row.id,
                chunking=dict(row.chunking or {}),
                indexed_documents=sum(
                    1
                    for document in self._db.documents.values()
                    if document.connector_id == row.id and document.status == "indexed"
                ),
            )
            for row in sorted(
                self._db.connectors.values(), key=lambda row: (row.organization_id, row.id)
            )
            if row.status != "deleting"
        ]

    async def indexed_documents(self, connector_id: uuid.UUID) -> list[uuid.UUID]:
        return sorted(
            row.id
            for row in self._db.documents.values()
            if row.connector_id == connector_id and row.status == "indexed"
        )

    async def fact_ids(self, organization_id: uuid.UUID) -> set[str]:
        return {
            str(row.id)
            for row in self._db.memory_facts.values()
            if row.organization_id == organization_id
        }

    async def document_uris(self, organization_id: uuid.UUID) -> set[str]:
        return {
            str(row.source_uri)
            for row in self._db.documents.values()
            if row.organization_id == organization_id
        }

    async def storage_prefixes(self, organization_id: uuid.UUID) -> list[str]:
        return [
            row.storage_prefix
            for row in self._db.connectors.values()
            if row.organization_id == organization_id and row.storage_prefix
        ]

    async def unfinished(self, job: str) -> RunState | None:
        found = [
            run
            for run in self._db.maintenance_runs.values()
            if run.job == job and run.status == "running"
        ]
        found.sort(key=lambda run: run.started_at, reverse=True)
        return _state_of(found[0]) if found else None

    async def open_run(self, job: str) -> RunState:
        run = MaintenanceRun(
            id=uuid7(),
            job=job,
            status="running",
            started_at=datetime.now(UTC),
            cursor={},
            report={},
        )
        self._db.maintenance_runs[run.id] = run
        return _state_of(run)

    async def save_run(
        self,
        run_id: uuid.UUID,
        *,
        cursor: Mapping[str, Any] | None = None,
        report: Mapping[str, Any] | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        run = self._db.maintenance_runs.get(run_id)
        if run is None:
            return
        if cursor is not None:
            run.cursor = dict(cursor)
        if report is not None:
            run.report = dict(report)
        if status is not None:
            run.status = status
            if status != "running":
                run.finished_at = datetime.now(UTC)
        if error is not None:
            run.error = error

    async def recent_runs(self, *, limit: int = 20) -> list[RunState]:
        found = sorted(
            self._db.maintenance_runs.values(), key=lambda run: run.started_at, reverse=True
        )
        return [_state_of(run) for run in found[:limit]]

    # -- erasure ---------------------------------------------------------

    async def organization(self, organization_id: uuid.UUID) -> OrganizationRow | None:
        row = self._db.organizations.get(organization_id)
        if row is None:
            return None
        return OrganizationRow(
            id=row.id,
            name=row.name,
            slug=row.slug,
            status=row.status,
            purge_after=row.purge_after,
        )

    async def mark_deleting(self, organization_id: uuid.UUID, *, purge_after: datetime) -> None:
        row = self._db.organizations.get(organization_id)
        if row is not None:
            row.status = "deleting"
            row.purge_after = purge_after

    async def clear_deleting(self, organization_id: uuid.UUID) -> None:
        row = self._db.organizations.get(organization_id)
        if row is not None:
            row.status = "active"
            row.purge_after = None

    async def due_for_purge(self, now: datetime) -> list[uuid.UUID]:
        return sorted(
            row.id
            for row in self._db.organizations.values()
            if row.status == "deleting" and row.purge_after is not None and row.purge_after <= now
        )

    async def purge_organization(self, organization_id: uuid.UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label, rows in (
            ("transcripts", self._db.transcripts),
            ("request_logs", self._db.request_logs),
            ("distillation_runs", self._db.distillation_runs),
            ("summarization_runs", self._db.summarization_runs),
            ("evaluation_runs", self._db.evaluation_runs),
            ("evaluation_items", self._db.evaluation_items),
            ("evaluation_sets", self._db.evaluation_sets),
            ("index_audits", self._db.index_audits),
            ("reprocessing_runs", self._db.reprocessing_runs),
            ("memory_facts", self._db.memory_facts),
            ("end_users", self._db.end_users),
            ("documents", self._db.documents),
            ("connectors", self._db.connectors),
            ("api_keys", self._db.api_keys),
            ("gateway_targets", self._db.gateway_targets),
            ("gateways", self._db.gateways),
            ("upstream_models", self._db.upstream_models),
            ("invitations", self._db.invitations),
            ("users", self._db.users),
        ):
            removed = [
                key
                for key, row in rows.items()
                if getattr(row, "organization_id", None) == organization_id
            ]
            for key in removed:
                del rows[key]
            counts[label] = len(removed)
        counts["organizations"] = 1 if self._db.organizations.pop(organization_id, None) else 0
        return counts

    async def commit(self) -> None:
        return None


def _size_of(body: Transcript) -> int:
    """A stand-in for ``pg_column_size``, close enough for a report.

    Not exact and not trying to be: the number on the Maintenance screen answers "did
    that reclaim anything worth reclaiming", and a byte count that agreed with PostgreSQL's
    TOAST accounting to the byte would be a much larger claim than this makes.
    """
    total = len(body.response_body or "")
    for value in (body.request_body, body.assembled_prompt):
        if value is not None:
            total += len(str(value))
    return total


@dataclass
class MemoryMaintenanceStore:
    db: MemoryDatabase
    partitions: dict[str, set[date]] = field(default_factory=dict)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[MaintenanceTransaction]:
        yield MemoryMaintenanceTransaction(self.db, self.partitions)


__all__ = [
    "PARTITIONED_TABLES",
    "PURGE_ORDER",
    "ExpiredFact",
    "GatewayRetention",
    "MaintenanceStore",
    "MaintenanceTransaction",
    "MemoryMaintenanceStore",
    "MemoryMaintenanceTransaction",
    "OrganizationRow",
    "PostgresMaintenanceStore",
    "PostgresMaintenanceTransaction",
    "Pruned",
    "RunState",
    "create_partition_sql",
    "day_of",
    "partition_name",
]
