"""Reprocessing a connector's documents as a tracked run (task 104).

Task 20's reindex was correct and anonymous: it put every document back in the queue and
returned a count, and from that moment the operation existed only as jobs. Nobody could
ask whether it was still going, how far it had got, what it had cost, or whether the
worker that was running it had died with half of it done. This module gives the operation
a row, and the row is what every one of those questions is answered from.

**The jobs are still the state.** A run does not run anything itself. It claims the
documents in its scope — resets them to ``pending`` under its id — and enqueues the same
ingestion job an upload enqueues, tagged with the run. Each job's finish increments the
run's counters inside the transaction that writes the document row, so the counters are
exact; a run finishes when every document it claimed has settled. A worker restart loses
nothing that was still queued, and a worker that died mid-document leaves that document
owned by the run and unfinished, which is what :meth:`Reprocessor.continue_stalled`
re-enqueues. Continued, not restarted: the counters and the documents already done stay.

**Staleness is stored, and this module keeps it true.** The connector service marks
documents stale on every configuration save; ingestion marks each one current when it
finishes; and the nightly :meth:`Reprocessor.reconcile` recomputes every connector's
statuses from its fingerprints and logs any row the stored status had wrong — because a
status maintained by two writers is a status that will eventually be wrong somewhere, and
the fingerprint is the truth.

**Scope defaults to what is stale.** The old endpoint could only reprocess everything;
reprocessing a thousand current documents to recut twelve stale ones was its only way of
recutting the twelve. The estimate it shows first is the sum of what the claimed documents
cost last time, which is the honest guess and the one whose error the run's own
``spent_tokens`` column then reports.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.errors import NotFound, Validation
from app.core.ids import uuid7
from app.core.metrics import ReprocessingMetrics
from app.core.tenancy import Actor, TenantScope
from app.db.models import Connector, Document, ReprocessingRun
from app.db.models.reprocessing import REPROCESSING_SCOPES, REPROCESSING_TRIGGERS
from app.schemas.connector_config import ChunkingConfig, effective
from app.schemas.reprocessing import progress_of as _progress
from app.services.audit import summarize
from app.services.audit_snapshots import target_of
from app.services.connector_store import ConnectorStore, ReprocessScope
from app.services.filetypes import FORMAT_KINDS, format_label
from app.services.index_fingerprint import stale_reason
from app.services.jobs import INGEST_DOCUMENT, JobOutbox, JobQueue, ingest_key, queue_for
from app.services.reindex import Progress
from app.services.reprocessing_store import ReprocessingStore

logger = logging.getLogger(__name__)

#: Documents claimed and enqueued per transaction. The same bound as task 20's page, for
#: the same reason: one button press on a large connector is many short transactions.
PAGE = 200

#: A run with no finish this long after it started, whose documents have stopped moving,
#: is one a worker died under. Generous: a big PDF on a slow provider is minutes, and
#: continuing a run that is merely slow re-enqueues jobs the queue will deduplicate.
STALL_AFTER = timedelta(minutes=30)

#: Runs shown in a connector's history.
HISTORY = 20

#: What every document the reconciliation job re-marks is logged as.
_DISAGREED = "index status disagreed with the fingerprint"


class ExpectedFingerprints(Protocol):
    """Where the expected fingerprints come from: the pipeline, whose answer is the thing
    the rows are compared against. A port so the API and the worker inject the one they
    were built with. ``embedding_model`` overrides the serving embedder's for the one
    caller that knows better — the platform reindex, at the moment it adopts a model the
    process it runs in was not built with."""

    def __call__(
        self, connector: Connector, *, embedding_model: str | None = None
    ) -> Awaitable[Mapping[str, str]]: ...


@dataclass(frozen=True, slots=True)
class Started:
    """What ``POST /connectors/{id}/reprocess`` returns: the run, and whether this call
    created it or found one already going."""

    run: ReprocessingRun
    created: bool


@dataclass(frozen=True, slots=True)
class StaleAlert:
    """A connector whose documents have been stale for longer than the threshold — the
    dashboard's degraded state (task 104), with the age so the entry says how long."""

    connector_id: uuid.UUID
    connector_name: str
    stale_documents: int
    stale_since: datetime
    age_hours: float


#: How long documents may sit stale before the dashboard calls the connector degraded.
#: A day: a change is expected to be followed by its reprocess the same working day, and
#: a connector still stale tomorrow is one somebody forgot.
STALE_ALERT_AFTER = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What the nightly pass found."""

    connectors: int = 0
    #: Rows whose stored status disagreed with their fingerprint, re-marked.
    disagreements: int = 0
    #: Runs a dead worker had left open, continued.
    continued: int = 0
    #: Runs closed because nothing they owned was left to finish.
    closed: int = 0
    stale_by_connector: dict[uuid.UUID, int] = field(default_factory=dict)


def progress_of(run: ReprocessingRun, *, now: datetime | None = None) -> Progress:
    """Documents settled over documents claimed, with an ETA at the observed rate — task
    17's :class:`Progress`, in documents rather than points, so both screens read alike.
    The arithmetic lives with the response body; this is the service-side view of it."""
    progress = _progress(run, now=now)
    return Progress(done=progress.done, total=progress.total, eta_seconds=progress.eta_seconds)


class Reprocessor:
    def __init__(
        self,
        store: ReprocessingStore,
        *,
        connectors: ConnectorStore,
        expected: ExpectedFingerprints,
        queue: JobQueue,
        metrics: ReprocessingMetrics | None = None,
        stall_after: timedelta = STALL_AFTER,
    ) -> None:
        self._store = store
        self._connectors = connectors
        self._expected = expected
        self._queue = queue
        self._metrics = metrics
        self._stall_after = stall_after

    # -- reads ------------------------------------------------------------

    async def running(self, actor: Actor, connector_id: uuid.UUID) -> ReprocessingRun | None:
        async with self._store.begin(actor.scope) as transaction:
            return await transaction.running(connector_id)

    async def runs(
        self, actor: Actor, connector_id: uuid.UUID, *, limit: int = HISTORY
    ) -> list[ReprocessingRun]:
        async with self._connectors.begin(actor.scope) as connectors:
            if await connectors.connector(connector_id) is None:
                raise NotFound("Connector not found.")
        async with self._store.begin(actor.scope) as transaction:
            return list(await transaction.runs(connector_id, limit=max(1, min(limit, 100))))

    async def run(self, actor: Actor, run_id: uuid.UUID) -> ReprocessingRun:
        async with self._store.begin(actor.scope) as transaction:
            run = await transaction.find(run_id)
        if run is None:
            raise NotFound("No such reprocessing run.")
        return run

    async def alerts(
        self,
        actor: Actor,
        *,
        older_than: timedelta = STALE_ALERT_AFTER,
        now: datetime | None = None,
    ) -> list[StaleAlert]:
        """Connectors whose documents have been stale for longer than ``older_than``."""
        moment = now or datetime.now(UTC)
        async with self._connectors.begin(actor.scope) as transaction:
            summaries = await transaction.stale_summaries()
        return [
            StaleAlert(
                connector_id=row.id,
                connector_name=row.name,
                stale_documents=row.stale,
                stale_since=row.since,
                age_hours=round((moment - row.since).total_seconds() / 3600, 1),
            )
            for row in summaries
            if moment - row.since >= older_than
        ]

    async def spawned_by(self, reindex_run_id: uuid.UUID) -> list[ReprocessingRun]:
        """The per-connector runs a platform reindex created — read across organizations,
        because the platform screen is the one asking."""
        async with self._store.begin(_PLATFORM) as transaction:
            return list(await transaction.spawned_by(reindex_run_id))

    # -- starting ---------------------------------------------------------

    async def start(
        self,
        actor: Actor,
        connector_id: uuid.UUID,
        *,
        scope: ReprocessScope | None = None,
        trigger: str | None = None,
    ) -> Started:
        """Start a run over the connector's documents in ``scope``, or return the one
        already going. One run per connector at a time: two would fight over the same
        rows, and the person's next question is always "what is already running".
        """
        scope = scope or ReprocessScope()
        if scope.kind not in REPROCESSING_SCOPES:
            raise Validation(
                f"'{scope.kind}' is not a scope. Available: {', '.join(REPROCESSING_SCOPES)}.",
                param="scope",
            )
        if scope.kind == "formats":
            unknown = sorted(scope.formats - set(FORMAT_KINDS))
            if unknown or not scope.formats:
                raise Validation(
                    f"{', '.join(unknown) or 'nothing'}: not a format this build classifies. "
                    f"Available: {', '.join(FORMAT_KINDS)}.",
                    param="formats",
                )
        if trigger is not None and trigger not in REPROCESSING_TRIGGERS:
            raise Validation(
                f"'{trigger}' is not a trigger. Available: {', '.join(REPROCESSING_TRIGGERS)}.",
                param="trigger",
            )

        async with self._connectors.begin(actor.scope) as connectors:
            connector = await connectors.connector(connector_id)
            if connector is None:
                raise NotFound("Connector not found.")
            organization_id = connector.organization_id
        async with self._store.begin(actor.scope) as transaction:
            existing = await transaction.running(connector_id)
        if existing is not None:
            return Started(run=existing, created=False)

        run = ReprocessingRun(
            id=uuid7(),
            organization_id=organization_id,
            connector_id=connector_id,
            trigger=trigger or "manual",
            scope=scope.kind,
            formats=sorted(scope.formats),
            requested_by=actor.user_id,
            requested_by_label=actor.label,
            status="running",
            total=0,
            done=0,
            failed=0,
            skipped=0,
            estimated_tokens=0,
            spent_tokens=0,
            resumed=0,
            report={},
            started_at=datetime.now(UTC),
        )
        async with self._store.begin(actor.scope) as transaction:
            await transaction.add(run)
            await transaction.commit()

        # Claim first, count, *then* enqueue. The run's total has to be right before the
        # first job can finish, or the first finish would find `settled >= total` and
        # close a run that had barely started.
        claimed, estimate, reasons = await self._claim(actor.scope, connector_id, run.id, scope)
        inferred = trigger or _infer_trigger(reasons)
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.find(run.id)
            assert row is not None
            row.total = claimed
            row.estimated_tokens = estimate
            row.trigger = inferred
            row.report = {"stale_reasons": dict(reasons)} if reasons else {}
            if claimed == 0:
                row.status = "succeeded"
                row.finished_at = datetime.now(UTC)
            transaction.audit(
                actor,
                "connector.reprocess",
                target=target_of(connector),
                organization_id=organization_id,
                summary=summarize(
                    claimed, scope=scope.kind, formats=sorted(scope.formats), trigger=inferred
                ),
            )
            await transaction.commit()
            run = row

        queued = await self._enqueue_owned(actor.scope, run, suffix="")
        await self._gauge(actor.scope, connector_id)
        if self._metrics is not None and claimed == 0:
            self._metrics.runs.labels(outcome="succeeded").inc()
        logger.info(
            "reprocessing run started",
            extra={
                "connector_id": str(connector_id),
                "run_id": str(run.id),
                "scope": scope.kind,
                "documents": claimed,
                "queued": queued,
                "trigger": inferred,
                "audit_action": "connector.reprocess",
            },
        )
        return Started(run=run, created=True)

    async def retry_failed(self, actor: Actor, run_id: uuid.UUID) -> Started:
        """**Retry failed**: a new run over exactly the documents this one left failed."""
        previous = await self.run(actor, run_id)
        if previous.finished_at is None:
            raise Validation("This run is still going; wait for it to finish first.")
        return await self.start(
            actor,
            previous.connector_id,
            scope=ReprocessScope(kind="failed", run_id=previous.id),
            trigger=previous.trigger,
        )

    async def _claim(
        self,
        scope: TenantScope,
        connector_id: uuid.UUID,
        run_id: uuid.UUID,
        wanted: ReprocessScope,
    ) -> tuple[int, int, Counter[str]]:
        """Reset every document in scope under the run, a page at a time. Returns how
        many, the token estimate for them, and why each stale one was stale."""
        claimed = 0
        estimate = 0
        reasons: Counter[str] = Counter()
        after: uuid.UUID | None = None
        expected: Mapping[str, str] | None = None
        chunking: ChunkingConfig | None = None
        while True:
            async with self._connectors.begin(scope) as transaction:
                if expected is None:
                    connector = await transaction.connector(connector_id)
                    if connector is None:
                        raise NotFound("Connector not found.")
                    expected = await self._expected(connector)
                    chunking = ChunkingConfig.load(connector.chunking)
                page = await transaction.documents_in_scope(
                    connector_id, wanted, after=after, limit=PAGE
                )
                if not page:
                    break
                after = page[-1].id
                for document in page:
                    kind = format_label(document.mime_type or "")
                    reason = stale_reason(document.index_fingerprint, expected.get(kind, ""))
                    if reason is not None:
                        reasons[reason] += 1
                    estimate += _estimate(document, chunking, kind)
                await transaction.claim_for_run(page, run_id)
                await transaction.commit()
                claimed += len(page)
                if len(page) < PAGE:
                    break
        return claimed, estimate, reasons

    async def _enqueue_owned(self, scope: TenantScope, run: ReprocessingRun, *, suffix: str) -> int:
        """Enqueue an ingestion for every unfinished document the run owns. The start and
        the continuation are the same operation with a different key suffix, so a
        continued run's jobs are not deduplicated against the ones the dead worker took."""
        queued = 0
        async with self._connectors.begin(scope) as transaction:
            owned = await transaction.unfinished_for_run(run.id)
        outbox = JobOutbox(self._queue)
        for document in owned:
            outbox.add(
                INGEST_DOCUMENT,
                {
                    "organization_id": str(document.organization_id),
                    "document_id": str(document.id),
                    "run_id": str(run.id),
                },
                idempotency_key=(
                    f"{ingest_key(document.id, document.content_hash)}:reprocess:{run.id}{suffix}"
                ),
                queue=queue_for(document.source_name),
            )
            queued += 1
        await outbox.flush()
        return queued

    # -- the platform reindex's runs (task 17) -----------------------------

    async def open_for_reindex(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        reindex_run_id: uuid.UUID,
        documents: Sequence[uuid.UUID],
    ) -> ReprocessingRun:
        """A run for the documents a platform reindex is about to recut.

        The recut writes into the collection being built beside the live one, so the rows
        are *not* reset to ``pending`` — they are still indexed, in the collection that is
        still serving. They are marked ``reprocessing`` under the run, and each one comes
        back ``current`` as the reindexer settles it (:meth:`settle_recut`).
        """
        scope = TenantScope.of_organization(organization_id)
        run = ReprocessingRun(
            id=uuid7(),
            organization_id=organization_id,
            connector_id=connector_id,
            trigger="embedding_model",
            scope="all",
            formats=[],
            requested_by=None,
            requested_by_label="platform reindex",
            reindex_run_id=reindex_run_id,
            status="running",
            total=len(documents),
            done=0,
            failed=0,
            skipped=0,
            estimated_tokens=0,
            spent_tokens=0,
            resumed=0,
            report={},
            started_at=datetime.now(UTC),
        )
        async with self._store.begin(scope) as transaction:
            await transaction.add(run)
            if not documents:
                run.status = "succeeded"
                run.finished_at = datetime.now(UTC)
            await transaction.commit()
        async with self._connectors.begin(scope) as transaction:
            for document_id in documents:
                document = await transaction.document(document_id)
                if document is not None:
                    document.index_status = "reprocessing"
                    document.reprocessing_run_id = run.id
            await transaction.commit()
        return run

    async def settle_recut(
        self,
        *,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        document_id: uuid.UUID,
        outcome: str,
        tokens: int = 0,
        chunk_count: int | None = None,
    ) -> None:
        """One document of a reindex-spawned run was recut (or could not be)."""
        scope = TenantScope.of_organization(organization_id)
        async with self._connectors.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is not None:
                # Current: the row's fingerprint still names the model the live
                # collection holds, and at the swap the store rewrites the model segment
                # for every row at once. If the reindex fails first, nothing is wrong.
                document.index_status = "current"
                document.reprocessing_run_id = None
                if chunk_count is not None and outcome == "done":
                    document.chunk_count = chunk_count
            run = await transaction.count_reprocessed(run_id, outcome, tokens=tokens)
            await transaction.commit()
        if run is not None and run.finished_at is not None and self._metrics is not None:
            self._observe(run)

    async def adopt_embedding_model(self, organization_id: uuid.UUID, embedding_model: str) -> None:
        """After the platform reindex swapped this organization's collection: every
        indexed row now holds vectors from ``embedding_model``. Rewrite the rows and
        re-mark their statuses against fingerprints computed under that model."""
        scope = TenantScope.of_organization(organization_id)
        async with self._connectors.begin(scope) as transaction:
            rewritten = await transaction.rewrite_embedding_model(embedding_model)
            await transaction.commit()
        async with self._store.begin(scope) as transaction:
            connectors = list(await transaction.connectors())
        changed = 0
        for connector in connectors:
            expected = await self._expected(connector, embedding_model=embedding_model)
            async with self._connectors.begin(scope) as transaction:
                changed += await transaction.reconcile_index_status(connector.id, expected)
                await transaction.commit()
        logger.info(
            "documents re-marked under the adopted embedding model",
            extra={
                "organization_id": str(organization_id),
                "embedding_model": embedding_model,
                "rows": rewritten,
                "status_changes": changed,
            },
        )

    # -- keeping it true ---------------------------------------------------

    async def mark_after_save(
        self, scope: TenantScope, connector: Connector, kinds: Sequence[str]
    ) -> int:
        """After a configuration save: re-mark the affected formats from their
        fingerprints. Returns how many rows changed status."""
        if not kinds:
            return 0
        expected = await self._expected(connector)
        async with self._connectors.begin(scope) as transaction:
            changed = await transaction.reconcile_index_status(
                connector.id, expected, kinds=list(kinds)
            )
            await transaction.commit()
        await self._gauge(scope, connector.id)
        return changed

    async def _gauge(self, scope: TenantScope, connector_id: uuid.UUID) -> None:
        """Set ``documents_stale{connector}`` from the rows — after a save, after a run
        claims them, and nightly — so the alert follows the count without a scrape of the
        table on every request."""
        if self._metrics is None:
            return
        async with self._connectors.begin(scope) as transaction:
            counts = await transaction.index_status_counts([connector_id])
        found = counts.get(connector_id)
        self._metrics.stale.labels(connector=str(connector_id)).set(found.stale if found else 0)

    async def reconcile(self, *, now: datetime | None = None) -> ReconcileReport:
        """The nightly pass: every connector's statuses recomputed from its fingerprints,
        every stalled run continued, and the stale gauge set."""
        moment = now or datetime.now(UTC)
        report_connectors = 0
        disagreements = 0
        stale_by_connector: dict[uuid.UUID, int] = {}
        async with self._store.begin(_PLATFORM) as transaction:
            connectors = list(await transaction.connectors())
        for connector in connectors:
            scope = TenantScope.of_organization(connector.organization_id)
            try:
                expected = await self._expected(connector)
            except Exception:
                logger.exception(
                    "could not compute expected fingerprints",
                    extra={"connector_id": str(connector.id)},
                )
                continue
            async with self._connectors.begin(scope) as tx:
                changed = await tx.reconcile_index_status(connector.id, expected)
                counts = await tx.index_status_counts([connector.id])
                await tx.commit()
            report_connectors += 1
            disagreements += changed
            stale = counts.get(connector.id)
            stale_by_connector[connector.id] = stale.stale if stale else 0
            if changed:
                logger.warning(
                    _DISAGREED,
                    extra={"connector_id": str(connector.id), "documents": changed},
                )
            if self._metrics is not None:
                self._metrics.stale.labels(connector=str(connector.id)).set(
                    stale.stale if stale else 0
                )
        continued, closed = await self.continue_stalled(now=moment)
        return ReconcileReport(
            connectors=report_connectors,
            disagreements=disagreements,
            continued=continued,
            closed=closed,
            stale_by_connector=stale_by_connector,
        )

    async def continue_stalled(self, *, now: datetime | None = None) -> tuple[int, int]:
        """Continue every run a worker died under. Returns ``(continued, closed)``.

        A run is stalled when it is open, older than :data:`STALL_AFTER`, and still owns
        unfinished documents — those are re-enqueued under a key the queue has not seen.
        A run that is open and owns nothing unfinished has nothing left to wait for: its
        documents were deleted, or finished under another path, and it is closed at its
        counters rather than left running forever.
        """
        moment = now or datetime.now(UTC)
        continued = closed = 0
        async with self._store.begin(_PLATFORM) as transaction:
            open_runs = [
                run
                for run in await transaction.unfinished()
                if moment - run.started_at >= self._stall_after
            ]
        for run in open_runs:
            scope = TenantScope.of_organization(run.organization_id)
            if run.reindex_run_id is not None:
                # The platform reindex drives its own runs synchronously; a dead one is
                # the reindex's to resume, and re-enqueuing ingestion jobs here would
                # recut into the live collection instead of the one being built.
                continue
            async with self._connectors.begin(scope) as tx:
                owned = await tx.unfinished_for_run(run.id)
            async with self._store.begin(scope) as transaction:
                row = await transaction.find(run.id)
                if row is None:
                    continue
                if not owned:
                    row.total = row.settled
                    row.finished_at = moment
                    row.status = "succeeded" if row.failed == 0 else "partial"
                    await transaction.commit()
                    closed += 1
                    logger.info(
                        "reprocessing run closed with nothing left to finish",
                        extra={"run_id": str(run.id), "documents": row.settled},
                    )
                    continue
                row.resumed += 1
                await transaction.commit()
                resumed = row.resumed
            queued = await self._enqueue_owned(scope, run, suffix=f":resume{resumed}")
            continued += 1
            logger.warning(
                "reprocessing run continued after a stall",
                extra={"run_id": str(run.id), "documents": queued, "resumed": resumed},
            )
        return continued, closed

    def observe_finish(self, run: ReprocessingRun) -> None:
        """Record a finished run in the metrics. Called by the job handler after the
        finish that closed it; here so the counters live in one place."""
        self._observe(run)

    def _observe(self, run: ReprocessingRun) -> None:
        if self._metrics is None or run.finished_at is None:
            return
        self._metrics.runs.labels(outcome=run.status).inc()
        self._metrics.duration.observe((run.finished_at - run.started_at).total_seconds())


def _estimate(document: Document, chunking: ChunkingConfig | None, kind: str) -> int:
    """What re-embedding this document is likely to cost, in tokens.

    A document that was indexed before costs about what it cost last time — its chunk
    count times its chunk size, which overstates by the last partial chunk and understates
    by the overlap. One that was never indexed is guessed from its size at four bytes a
    token, which is right for prose and generous for a PDF whose bytes are mostly not text.
    """
    if document.chunk_count > 0 and chunking is not None:
        return document.chunk_count * effective(chunking, kind).chunk_size
    return max(0, document.size_bytes // 4)


def _infer_trigger(reasons: Counter[str]) -> str:
    """The run's trigger, from why its documents were stale: the commonest reason, or
    ``manual`` when nothing was."""
    for reason, _ in reasons.most_common():
        if reason in REPROCESSING_TRIGGERS:
            return reason
    return "manual"


_PLATFORM = TenantScope(role="superadmin", organization_id=None)

__all__ = [
    "FINGERPRINT_TTL_SECONDS",
    "HISTORY",
    "PAGE",
    "STALE_ALERT_AFTER",
    "STALL_AFTER",
    "CachedFingerprints",
    "ReconcileReport",
    "Reprocessor",
    "StaleAlert",
    "Started",
    "progress_of",
]


class CachedFingerprints:
    """Retrieval's :class:`~app.services.retrieval.FingerprintSource` (task 104).

    The connectors' effective fingerprints, read through the connector store and the
    pipeline and remembered for :data:`FINGERPRINT_TTL_SECONDS`. A request labels each
    retrieved chunk against these; a label thirty seconds behind a configuration save is a
    fine label, and a database read on every request is not a fine price for one.
    """

    def __init__(
        self,
        connectors: ConnectorStore,
        *,
        expected: ExpectedFingerprints,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._connectors = connectors
        self._expected = expected
        self._ttl = ttl_seconds
        self._clock = clock or time.monotonic
        self._cache: dict[uuid.UUID, tuple[float, Mapping[str, str]]] = {}

    async def expected(
        self, organization_id: uuid.UUID, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[str, Mapping[str, str]]:
        now = self._clock()
        found: dict[str, Mapping[str, str]] = {}
        missing: list[uuid.UUID] = []
        for connector_id in connector_ids:
            cached = self._cache.get(connector_id)
            if cached is not None and now - cached[0] < self._ttl:
                found[str(connector_id)] = cached[1]
            else:
                missing.append(connector_id)
        if missing:
            scope = TenantScope.of_organization(organization_id)
            async with self._connectors.begin(scope) as transaction:
                rows = [
                    row
                    for connector_id in missing
                    if (row := await transaction.connector(connector_id)) is not None
                ]
            for row in rows:
                fingerprints = dict(await self._expected(row))
                self._cache[row.id] = (now, fingerprints)
                found[str(row.id)] = fingerprints
        return found

    def forget(self, connector_id: uuid.UUID) -> None:
        """Drop one connector's entry — after a save in the same process, so the label
        follows the change without waiting out the TTL."""
        self._cache.pop(connector_id, None)


FINGERPRINT_TTL_SECONDS = 30.0
