"""The scheduled jobs: keep partitions ahead of need, enforce retention, find orphans.

Everything here is a decision; the statements that carry it out are in
:mod:`app.services.maintenance_store`. The split is the usual one in this codebase, and it
earns its keep especially well for this task: "which days should exist", "which gateway's
bodies are past their window" and "which vectors have no row" are all questions with exact
answers that a test can pin down in microseconds, and all three would otherwise only be
answerable by standing up PostgreSQL, Qdrant and an object store together.

Four decisions are worth stating.

**Runway is created ahead, and the alert is part of the job rather than beside it.** A
missing partition is not a slow query, it is an ``INSERT`` that fails — and the thing that
fails is request logging, which means the first symptom is silence on the monitoring
screen. So the pass creates thirty days ahead every night, and reports how many days are
actually there, and the number is a gauge as well as a row. The task 07 migration also
leaves a ``_default`` partition in place as the net under all of this; nothing here removes
it.

**Retention is two-stage, because ``retention_days`` is per gateway and a partition is
global.** A whole day is dropped only once it is older than the *longest* metadata window
any gateway has — that is the cheap case and it reclaims disk instantly. Inside the days
that are still live, each gateway's own windows are applied by predicate. So two gateways
sharing a partition are each honoured exactly, and the common case is still a ``DROP``.

**A pruning pass advances a floor and remembers it.** The unit of work is one
``(gateway, day)``, which is idempotent — it deletes by predicate, so running it twice
deletes nothing the second time — and the cursor exists purely so that the steady state is
one or two days per gateway per night rather than a year of empty deletes. Losing the
cursor costs time and nothing else, which is the property that makes it safe to keep in a
column nobody transactionally guards.

**The sweeper reports before it deletes, and it will not touch anything recent.** Both
halves matter. Report-first is because the first version of a sweeper is usually wrong in
one direction and the wrong direction destroys customer data; the age floor is because an
upload in flight *is* an object with no document row, and a sweeper without it would race
every upload it ever saw.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.metrics import MaintenanceMetrics
from app.schemas.platform import PlatformSettings
from app.services.fact_vectors import FactVectorStore
from app.services.maintenance_store import (
    PARTITIONED_TABLES,
    GatewayRetention,
    MaintenanceStore,
    MaintenanceTransaction,
    Pruned,
    RunState,
)
from app.services.object_store import ObjectStore
from app.services.platform_settings import effective_retention
from app.services.vector_backends import VectorBackends
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

#: Days of partitions kept ahead of today. Matches
#: :data:`app.db.models.request_log.PARTITION_DAYS_AHEAD`, which the task 07 migration
#: creates once; this is what keeps it rolling.
RUNWAY_DAYS = 30

#: Below this, the runway is reported low and the gauge goes with it. Seven days is a week
#: of holiday: long enough that somebody is back before the first insert fails.
LOW_RUNWAY_DAYS = 7

#: One day behind as well as ahead, so a row written by a worker with a slow clock still
#: lands in a real partition rather than the default one.
DAYS_BEHIND = 1

#: Expired facts removed per pass. A ceiling rather than "all of them": this runs beside
#: live traffic and a tenant that let a million facts expire should not turn one night's
#: pass into an hour of Qdrant deletes.
FACT_BATCH = 500

#: Pause between units of work, so pruning cannot crowd out serving. Small, and applied
#: per ``(gateway, day)`` rather than per row — the statements are already bounded, and
#: this is about not monopolising a connection rather than about throttling rows.
BATCH_PAUSE_SECONDS = 0.05

#: How recently an object may have been written and still be considered an orphan. An
#: upload that has landed in the store but whose document row is not committed yet looks
#: exactly like an orphan, and it is not one.
ORPHAN_MIN_AGE_SECONDS = 3600

PARTITIONS = "partitions"
RETENTION = "retention"
SWEEP = "sweep"


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def missing_days(
    existing: Sequence[date], *, today: date, ahead: int = RUNWAY_DAYS, behind: int = DAYS_BEHIND
) -> list[date]:
    """Days in the window that have no partition, oldest first."""
    have = set(existing)
    start = today - timedelta(days=behind)
    wanted = (start + timedelta(days=offset) for offset in range(behind + ahead + 1))
    return [day for day in wanted if day not in have]


def days_ahead(existing: Sequence[date], *, today: date) -> int:
    """How many consecutive days from today forward are covered.

    Consecutive on purpose. A deployment with a partition for today and another for a day
    next month has one day of runway, not thirty: the gap is where inserts start failing,
    and a count of rows in ``pg_inherits`` would report the reassuring number.
    """
    have = set(existing)
    count = 0
    while today + timedelta(days=count) in have:
        count += 1
    return count


def expired_days(existing: Sequence[date], *, today: date, keep_days: int) -> list[date]:
    """Partitions entirely older than the longest retention anybody has."""
    cutoff = today - timedelta(days=keep_days)
    return sorted(day for day in existing if day < cutoff)


def effective_windows(retention: GatewayRetention, config: PlatformSettings) -> tuple[int, int]:
    """One gateway's windows after the platform ceilings.

    Capped rather than refused, for the reason
    :func:`~app.services.platform_settings.effective_retention` gives: lowering a ceiling
    must not make the job skip gateways that were configured under the old one — it must
    make them stricter, tonight.
    """
    bodies, metadata, _ = effective_retention(config, retention.body_days, retention.metadata_days)
    return bodies, metadata


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Runway:
    table: str
    days_ahead: int
    last_day: date | None
    threshold: int = LOW_RUNWAY_DAYS

    @property
    def low(self) -> bool:
        return self.days_ahead < self.threshold

    def as_json(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "days_ahead": self.days_ahead,
            "last_day": self.last_day.isoformat() if self.last_day else None,
            "low": self.low,
        }


@dataclass
class PartitionReport:
    created: dict[str, list[str]] = field(default_factory=dict)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    runway: list[Runway] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "created": {table: list(days) for table, days in self.created.items()},
            "dropped": {table: list(days) for table, days in self.dropped.items()},
            "runway": [entry.as_json() for entry in self.runway],
            "low_runway": [entry.table for entry in self.runway if entry.low],
        }


@dataclass
class GatewayPruned:
    gateway_id: uuid.UUID
    name: str
    organization_id: uuid.UUID
    bodies: Pruned = field(default_factory=Pruned)
    rows: Pruned = field(default_factory=Pruned)

    def as_json(self) -> dict[str, Any]:
        return {
            "gateway_id": str(self.gateway_id),
            "gateway": self.name,
            "organization_id": str(self.organization_id),
            "bodies_removed": self.bodies.rows,
            "bytes_reclaimed": self.bodies.bytes + self.rows.bytes,
            "rows_removed": self.rows.rows,
        }


@dataclass
class RetentionReport:
    gateways: list[GatewayPruned] = field(default_factory=list)
    partitions: PartitionReport = field(default_factory=PartitionReport)
    facts_expired: int = 0
    #: Facts whose row went but whose vector could not be removed. Counted separately and
    #: never folded into the number above: an orphaned vector is a fact that still reaches
    #: an answer after it expired, so "we deleted 40" would be the wrong claim.
    facts_stranded: int = 0

    @property
    def rows_removed(self) -> int:
        return sum(entry.rows.rows for entry in self.gateways)

    @property
    def bodies_removed(self) -> int:
        return sum(entry.bodies.rows for entry in self.gateways)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(entry.bodies.bytes + entry.rows.bytes for entry in self.gateways)

    def as_json(self) -> dict[str, Any]:
        return {
            "gateways": [entry.as_json() for entry in self.gateways],
            "partitions": self.partitions.as_json(),
            "facts_expired": self.facts_expired,
            "facts_stranded": self.facts_stranded,
            "rows_removed": self.rows_removed,
            "bodies_removed": self.bodies_removed,
            "bytes_reclaimed": self.bytes_reclaimed,
        }


@dataclass(frozen=True, slots=True)
class OrphanSet:
    store: str
    kind: str
    ids: tuple[str, ...]

    def as_json(self, *, sample: int = 10) -> dict[str, Any]:
        return {
            "store": self.store,
            "kind": self.kind,
            "count": len(self.ids),
            "sample": list(self.ids[:sample]),
        }


@dataclass
class SweepReport:
    applied: bool = False
    organizations: int = 0
    groups: list[OrphanSet] = field(default_factory=list)
    deleted: int = 0

    @property
    def total(self) -> int:
        return sum(len(group.ids) for group in self.groups)

    def as_json(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "organizations": self.organizations,
            "orphans": self.total,
            "deleted": self.deleted,
            "groups": [group.as_json() for group in self.groups],
        }


# ---------------------------------------------------------------------------
# partitions
# ---------------------------------------------------------------------------


class PartitionManager:
    """Creates tomorrow's partitions and drops the ones nothing may read any more."""

    def __init__(
        self,
        store: MaintenanceStore,
        *,
        runway_days: int = RUNWAY_DAYS,
        threshold_days: int = LOW_RUNWAY_DAYS,
        metrics: MaintenanceMetrics | None = None,
    ) -> None:
        self._store = store
        self._runway_days = runway_days
        self._threshold = threshold_days
        self._metrics = metrics

    async def runway(self) -> list[Runway]:
        today = _today()
        async with self._store.begin() as transaction:
            return [
                await self._runway_of(transaction, table, today) for table in PARTITIONED_TABLES
            ]

    async def _runway_of(
        self, transaction: MaintenanceTransaction, table: str, today: date
    ) -> Runway:
        existing = await transaction.partition_days(table)
        entry = Runway(
            table=table,
            days_ahead=days_ahead(existing, today=today),
            last_day=existing[-1] if existing else None,
            threshold=self._threshold,
        )
        if self._metrics is not None:
            self._metrics.runway.labels(table=table).set(entry.days_ahead)
        if entry.low:
            logger.error(
                "partition runway is low; request logging will start failing",
                extra={"table": table, "days_ahead": entry.days_ahead},
            )
        return entry

    async def ensure(self, *, keep_days: int | None = None) -> PartitionReport:
        """Create what is missing and, when told how long to keep, drop what is expired.

        ``keep_days`` is passed in rather than read here because it is the *longest*
        retention any gateway has, and that is retention's question, not partitioning's.
        Called with ``None`` — from the standalone partition job — nothing is dropped,
        which is the safe default for a pass whose job is to add.
        """
        today = _today()
        report = PartitionReport()
        async with self._store.begin() as transaction:
            for table in PARTITIONED_TABLES:
                existing = await transaction.partition_days(table)
                created: list[str] = []
                for day in missing_days(existing, today=today, ahead=self._runway_days):
                    await transaction.create_partition(table, day)
                    created.append(day.isoformat())
                if created:
                    report.created[table] = created

                if keep_days is not None:
                    dropped: list[str] = []
                    for day in expired_days(existing, today=today, keep_days=keep_days):
                        await transaction.drop_partition(table, day)
                        dropped.append(day.isoformat())
                    if dropped:
                        report.dropped[table] = dropped
            await transaction.commit()

            report.runway = [
                await self._runway_of(transaction, table, today) for table in PARTITIONED_TABLES
            ]
        if report.created or report.dropped:
            # Not ``created``/``dropped``: ``created`` is a field ``logging.LogRecord``
            # already owns, and passing it in ``extra`` raises rather than being shadowed.
            logger.info(
                "partition maintenance finished",
                extra={
                    "partitions_created": report.created,
                    "partitions_dropped": report.dropped,
                },
            )
        return report


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


class RetentionJob:
    """SPEC §10.2's nightly pass, plus the expired-fact purge that goes with it."""

    def __init__(
        self,
        store: MaintenanceStore,
        *,
        partitions: PartitionManager,
        facts: FactVectorStore,
        config: PlatformSettings | None = None,
        pause_seconds: float = BATCH_PAUSE_SECONDS,
        fact_batch: int = FACT_BATCH,
        metrics: MaintenanceMetrics | None = None,
    ) -> None:
        self._store = store
        self._partitions = partitions
        self._facts = facts
        self._config = config or PlatformSettings()
        self._pause = pause_seconds
        self._fact_batch = fact_batch
        self._metrics = metrics

    def configured_with(self, config: PlatformSettings) -> RetentionJob:
        """The same job against a freshly-read platform configuration.

        The ceilings are read once per run rather than per gateway: a run that used
        different ceilings for the first tenant and the last would produce a report
        nobody could reconcile.
        """
        self._config = config
        return self

    async def run(self, *, now: datetime | None = None) -> RetentionReport:
        moment = now or datetime.now(UTC)
        today = moment.date()
        report = RetentionReport()

        async with self._store.begin() as transaction:
            run = await _adopt(transaction, RETENTION)
            await transaction.commit()
        floors = _floors(run)

        async with self._store.begin() as transaction:
            retentions = await transaction.retentions()
            existing = await transaction.partition_days("request_logs")

        # The whole-day drop comes first and is bounded by the *longest* window anybody
        # has, so it can never remove a day some other gateway is still entitled to.
        keep = max((effective_windows(entry, self._config)[1] for entry in retentions), default=0)
        report.partitions = await self._partitions.ensure(keep_days=keep or None)
        remaining = [day for day in existing if day not in _dropped(report.partitions)]

        for entry in retentions:
            body_days, metadata_days = effective_windows(entry, self._config)
            pruned = await self._prune(
                entry,
                days=remaining,
                today=today,
                body_days=body_days,
                metadata_days=metadata_days,
                floor=_as_day(floors.get(str(entry.gateway_id))),
            )
            report.gateways.append(pruned)
            floors[str(entry.gateway_id)] = _next_floor(today=today, metadata_days=metadata_days)
            async with self._store.begin() as transaction:
                await transaction.save_run(run.id, cursor={"floors": floors})
                await transaction.commit()

        expired, stranded = await self._expire_facts(moment)
        report.facts_expired = expired
        report.facts_stranded = stranded

        async with self._store.begin() as transaction:
            await transaction.save_run(
                run.id, report=report.as_json(), cursor={"floors": floors}, status="succeeded"
            )
            await transaction.commit()

        if self._metrics is not None:
            self._metrics.pruned_rows.inc(report.rows_removed + report.bodies_removed)
            self._metrics.pruned_bytes.inc(report.bytes_reclaimed)
        logger.info(
            "retention pass finished",
            extra={
                "rows_removed": report.rows_removed,
                "bodies_removed": report.bodies_removed,
                "bytes_reclaimed": report.bytes_reclaimed,
                "facts_expired": report.facts_expired,
            },
        )
        return report

    async def _prune(
        self,
        entry: GatewayRetention,
        *,
        days: Sequence[date],
        today: date,
        body_days: int,
        metadata_days: int,
        floor: date | None,
    ) -> GatewayPruned:
        result = GatewayPruned(
            gateway_id=entry.gateway_id, name=entry.name, organization_id=entry.organization_id
        )
        body_cutoff = today - timedelta(days=body_days)
        metadata_cutoff = today - timedelta(days=metadata_days)
        for day in sorted(days):
            if day >= body_cutoff:
                # Days newer than the body window are newer than the metadata window
                # too, since metadata is validated to be the longer of the pair. Nothing
                # after this point can match, so stopping is correct rather than merely
                # faster.
                break
            if floor is not None and day < floor:
                continue
            async with self._store.begin() as transaction:
                if day < metadata_cutoff:
                    result.rows += await transaction.prune_metadata(entry.gateway_id, day)
                else:
                    result.bodies += await transaction.prune_bodies(entry.gateway_id, day)
                await transaction.commit()
            if self._pause:
                await asyncio.sleep(self._pause)
        return result

    async def _expire_facts(self, now: datetime) -> tuple[int, int]:
        """Remove facts past ``expires_at`` from Qdrant **and** PostgreSQL.

        Vectors first, rows second — the same ordering every deletion in this codebase
        uses. A crash between the two leaves a fact with no vector, which is invisible to
        recall and gets cleaned up on the next pass; the other order leaves a vector with
        no row, which still shapes answers and which nothing would ever look for.
        """
        removed = 0
        stranded = 0
        async with self._store.begin() as transaction:
            expired = await transaction.expired_facts(now=now, limit=self._fact_batch)
        if not expired:
            return 0, 0

        by_organization: dict[uuid.UUID, list[uuid.UUID]] = {}
        for fact in expired:
            by_organization.setdefault(fact.organization_id, []).append(fact.fact_id)

        for organization_id, fact_ids in by_organization.items():
            try:
                await self._facts.delete(organization_id, fact_ids)
            except Exception:
                # The row is left in place deliberately. A row without its vector is
                # recoverable on the next pass; deleting the row anyway would strand the
                # vector permanently, and a stranded vector is a fact that keeps
                # answering questions after it expired.
                stranded += len(fact_ids)
                logger.warning(
                    "could not remove expired fact vectors; rows kept for the next pass",
                    extra={"organization_id": str(organization_id), "facts": len(fact_ids)},
                    exc_info=True,
                )
                continue
            async with self._store.begin() as transaction:
                removed += await transaction.delete_facts(fact_ids)
                await transaction.commit()
        return removed, stranded


# ---------------------------------------------------------------------------
# orphans
# ---------------------------------------------------------------------------


class OrphanSweeper:
    """Vectors and objects with no row behind them.

    Reads three stores and compares. Nothing is deleted unless ``apply`` is passed, and
    the report is produced either way — so the destructive pass is always run against a
    set somebody has already looked at.
    """

    def __init__(
        self,
        store: MaintenanceStore,
        *,
        vectors: VectorStore,
        backends: VectorBackends,
        facts: FactVectorStore,
        objects: ObjectStore,
        min_age_seconds: float = ORPHAN_MIN_AGE_SECONDS,
        metrics: MaintenanceMetrics | None = None,
    ) -> None:
        self._store = store
        self._vectors = vectors
        self._backends = backends
        self._facts = facts
        self._objects = objects
        self._min_age = min_age_seconds
        self._metrics = metrics

    async def sweep(
        self,
        *,
        apply: bool = False,
        organization_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> SweepReport:
        moment = now or datetime.now(UTC)
        report = SweepReport(applied=apply)

        async with self._store.begin() as transaction:
            run = await transaction.open_run(SWEEP)
            await transaction.commit()
            organizations = await transaction.organizations()

        wanted = [
            entry
            for entry in organizations
            if organization_id is None or entry[0] == organization_id
        ]
        report.organizations = len(wanted)

        for identifier, _name in wanted:
            report.groups.extend(await self._chunks(identifier, apply=apply, report=report))
            report.groups.extend(await self._facts_of(identifier, apply=apply, report=report))
            report.groups.extend(
                await self._objects_of(identifier, apply=apply, report=report, now=moment)
            )

        report.groups = [group for group in report.groups if group.ids]
        if self._metrics is not None:
            self._metrics.orphans.set(report.total)
        async with self._store.begin() as transaction:
            await transaction.save_run(run.id, report=report.as_json(), status="succeeded")
            await transaction.commit()
        logger.info(
            "orphan sweep finished",
            extra={"orphans": report.total, "deleted": report.deleted, "applied": apply},
        )
        return report

    async def _chunks(
        self, organization_id: uuid.UUID, *, apply: bool, report: SweepReport
    ) -> list[OrphanSet]:
        index = await self._backends.admin_for(organization_id)
        live = await index.live_collection(organization_id)
        if live is None:
            return []
        indexed = await index.documents_in(live)
        async with self._store.begin() as transaction:
            known = await transaction.document_ids(organization_id)
        orphans = tuple(sorted(indexed - known))
        if orphans and apply:
            for document_id in orphans:
                await self._vectors.delete_document(organization_id, uuid.UUID(document_id))
            report.deleted += len(orphans)
        return [
            OrphanSet(
                store=f"vectors:{await self._kind(organization_id)}",
                kind="document_points",
                ids=orphans,
            )
        ]

    async def _facts_of(
        self, organization_id: uuid.UUID, *, apply: bool, report: SweepReport
    ) -> list[OrphanSet]:
        indexed = await self._facts.ids(organization_id)
        if not indexed:
            return []
        async with self._store.begin() as transaction:
            known = await transaction.fact_ids(organization_id)
        orphans = tuple(sorted(indexed - known))
        if orphans and apply:
            await self._facts.delete(organization_id, [uuid.UUID(value) for value in orphans])
            report.deleted += len(orphans)
        return [
            OrphanSet(
                store=f"vectors:{await self._kind(organization_id)}",
                kind="fact_points",
                ids=orphans,
            )
        ]

    async def _kind(self, organization_id: uuid.UUID) -> str:
        """Which backend this tenant's points were swept in.

        In the report rather than left implicit, because "12 orphaned points" is a
        different investigation depending on where they are — and an operator reading a
        sweep across a platform with two backends needs to know which one to look at.
        """
        return await self._backends.kind_for(organization_id)

    async def _objects_of(
        self, organization_id: uuid.UUID, *, apply: bool, report: SweepReport, now: datetime
    ) -> list[OrphanSet]:
        async with self._store.begin() as transaction:
            prefixes = await transaction.storage_prefixes(organization_id)
            known = await transaction.document_uris(organization_id)
        if not prefixes:
            return []
        orphans: list[str] = []
        for prefix in prefixes:
            async for ref in self._objects.list(prefix):
                if ref.key in known or not self._old_enough(ref.modified_at, now):
                    continue
                orphans.append(ref.key)
        found = tuple(sorted(orphans))
        if found and apply:
            report.deleted += await self._objects.delete(list(found))
        return [OrphanSet(store="object-store", kind="objects", ids=found)]

    def _old_enough(self, modified_at: datetime | None, now: datetime) -> bool:
        if modified_at is None:
            # A store that does not report modification times cannot be swept safely on
            # age, and the safe reading of "unknown age" is "too new to touch".
            return False
        return (now - modified_at).total_seconds() >= self._min_age


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _today() -> date:
    return datetime.now(UTC).date()


async def _adopt(transaction: MaintenanceTransaction, job: str) -> RunState:
    """The unfinished run of this job, or a new one seeded from the last.

    Adopting is what makes a pass resumable: a job killed halfway left a ``running`` row
    with its floors in it, and starting a fresh run would mean re-walking every day it had
    already cleared. Seeding a *new* run from the last one is the same idea across nights.
    """
    existing = await transaction.unfinished(job)
    if existing is not None:
        logger.info("resuming an unfinished maintenance run", extra={"job": job})
        return existing
    previous = [run for run in await transaction.recent_runs(limit=20) if run.job == job]
    run = await transaction.open_run(job)
    if previous:
        await transaction.save_run(run.id, cursor=previous[0].cursor)
        return RunState(
            id=run.id,
            job=run.job,
            status=run.status,
            started_at=run.started_at,
            cursor=dict(previous[0].cursor),
        )
    return run


def _floors(run: RunState) -> dict[str, str]:
    stored = run.cursor.get("floors")
    if not isinstance(stored, dict):
        return {}
    return {str(key): str(value) for key, value in stored.items()}


def _next_floor(*, today: date, metadata_days: int) -> str:
    """Where the next pass should start for this gateway.

    The metadata cutoff, not "the last day processed": everything older has had both its
    bodies and its rows removed, so there is nothing there to find again. Deliberately not
    clamped to the days that exist — a partition dropped tomorrow must not pull the floor
    backwards.
    """
    return (today - timedelta(days=metadata_days)).isoformat()


def _as_day(value: str | None) -> date | None:
    """A stored floor back into a date, tolerating a cursor written by another build."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _dropped(report: PartitionReport) -> set[date]:
    return {date.fromisoformat(day) for days in report.dropped.values() for day in days}


__all__ = [
    "BATCH_PAUSE_SECONDS",
    "FACT_BATCH",
    "LOW_RUNWAY_DAYS",
    "ORPHAN_MIN_AGE_SECONDS",
    "PARTITIONS",
    "RETENTION",
    "RUNWAY_DAYS",
    "SWEEP",
    "GatewayPruned",
    "OrphanSet",
    "OrphanSweeper",
    "PartitionManager",
    "PartitionReport",
    "RetentionJob",
    "RetentionReport",
    "Runway",
    "SweepReport",
    "days_ahead",
    "effective_windows",
    "expired_days",
    "missing_days",
]
