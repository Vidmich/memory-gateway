"""Running an index audit (task 103): the scroll, the searches, the sample, and the row.

:mod:`app.services.index_audit` is the arithmetic; this is everything around it that
touches a store. One object with three jobs:

**Start** is control-plane work. It writes the ``running`` row, records the audit event —
the embedding audit can spend at the provider, so the click is attributable — and enqueues
the job. A second click while one is running finds the running row and returns it rather
than starting a second scroll over the same collection.

**Run** is the job. A chunking audit scrolls the organization's live collection once, as the
reindexer does, keeping the payloads of this connector's points and nothing else. An
embedding audit scrolls the same way but pulls the *vectors* down for the first
:data:`VECTOR_SCAN_LIMIT` points it meets — a hundred thousand vectors of fifteen hundred
floats is a transfer the audit does not need to make to know whether the width is right and
whether a tenth of them are padding — and then asks the index two kinds of question with a
sample of those vectors: *who is your nearest neighbour* (a search, so the store does the
work and the answer is the ranking a real request would get) and, if asked to spend, *what
would the model say about this text today* (a fresh embedding, compared to the stored one).

**Read** is the screen's half: the latest report of each kind for a connector, with how
old it is and what a drift check would cost, and the organization's red findings for the
dashboard.

Audits read. The one thing here that writes to anything other than ``index_audits`` is
nothing; the drift check's embedding calls produce vectors that are compared and dropped.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.errors import NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import Connector, IndexAudit
from app.db.models.validation import AUDIT_KINDS
from app.schemas.connector_config import ChunkingConfig
from app.services.audit_snapshots import target_of
from app.services.connector_store import ConnectorStore, DocumentAuditRow
from app.services.embeddings import Embedder
from app.services.index_audit import (
    CHUNKING,
    EMBEDDING,
    RED,
    DocumentRecord,
    DriftSample,
    Neighbour,
    PointRecord,
    audit_chunking,
    audit_embeddings,
    drift_cosines,
    report_json,
)
from app.services.index_audit_store import FAILED, RUNNING, SUCCEEDED, IndexAuditStore
from app.services.jobs import AUDIT_INDEX, JobOutbox, JobQueue, audit_key
from app.services.summarization import KIND_SOURCE, embedding_input
from app.services.vector_backends import VectorBackends
from app.services.vector_store import ChunkPoint

logger = logging.getLogger(__name__)

NO_SUCH_CONNECTOR = "No such connector."

#: Points per scroll page.
PAGE = 500
#: How many points the embedding audit pulls vectors for. The width, the norms and the
#: padding checks are over these; the rest of the collection is counted, not read.
VECTOR_SCAN_LIMIT = 5000
#: Chunks whose nearest neighbour is looked up. Each one is a search.
AGREEMENT_SAMPLE = 200
#: The drift check's default and ceiling, in chunks, and its ceiling in tokens — the
#: number the button quotes before it runs.
DRIFT_SAMPLE_DEFAULT = 100
DRIFT_SAMPLE_MAX = 1000
DRIFT_MAX_TOKENS = 200_000


@dataclass(frozen=True, slots=True)
class DriftEstimate:
    """What the drift check would cost: chunks re-embedded and roughly how many tokens."""

    points: int
    sample: int
    tokens: int


@dataclass(frozen=True, slots=True)
class AuditStatus:
    """The connector screen's Validation section: the latest of each kind."""

    chunking: IndexAudit | None
    embedding: IndexAudit | None
    drift_estimate: DriftEstimate


@dataclass(frozen=True, slots=True)
class AuditAlert:
    """A connector whose latest audit of some kind raised a red finding."""

    connector_id: uuid.UUID
    connector_name: str | None
    kind: str
    audit_id: uuid.UUID
    finding: str
    created_at: datetime


class IndexAuditor:
    def __init__(
        self,
        store: IndexAuditStore,
        *,
        connectors: ConnectorStore,
        backends: VectorBackends,
        embedder: Embedder,
        queue: JobQueue,
        page_size: int = PAGE,
    ) -> None:
        self._store = store
        self._connectors = connectors
        self._backends = backends
        self._embedder = embedder
        self._queue = queue
        self._page = page_size

    # -- control plane -----------------------------------------------------

    async def start(
        self,
        actor: Actor,
        connector_id: uuid.UUID,
        kind: str,
        *,
        drift_sample: int | None = None,
    ) -> IndexAudit:
        # The connector first: a caller who cannot see it gets the same 404 whatever
        # they spelled the kind as, rather than a 422 that confirms the connector exists.
        connector = await self._connector(actor.scope, connector_id)
        if kind not in AUDIT_KINDS:
            raise Validation(f"kind is one of {', '.join(AUDIT_KINDS)}.", param="kind")
        if drift_sample is not None and kind != EMBEDDING:
            raise Validation("Only an embedding audit re-embeds a sample.", param="drift_sample")
        if drift_sample is not None and not 1 <= drift_sample <= DRIFT_SAMPLE_MAX:
            raise Validation(
                f"drift_sample is between 1 and {DRIFT_SAMPLE_MAX}.", param="drift_sample"
            )
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            running = await transaction.running(connector_id, kind)
            if running is not None:
                return running
            audit = IndexAudit(
                id=uuid7(),
                organization_id=connector.organization_id,
                connector_id=connector_id,
                kind=kind,
                status=RUNNING,
                created_by=actor.user_id,
                drift_sample=drift_sample,
                points=0,
                report={},
                created_at=datetime.now(UTC),
            )
            await transaction.add(audit)
            transaction.audit(
                actor,
                "connector.audit",
                target=target_of(connector),
                summary={"kind": kind, "drift_sample": drift_sample},
            )
            outbox.add(
                AUDIT_INDEX,
                {"organization_id": str(connector.organization_id), "audit_id": str(audit.id)},
                idempotency_key=audit_key(audit.id),
            )
            await transaction.commit()
        await outbox.flush()
        logger.info(
            "index audit started",
            extra={
                "audit_id": str(audit.id),
                "connector_id": str(connector_id),
                "kind": kind,
                "drift_sample": drift_sample,
                "audit_action": "connector.audit",
            },
        )
        return audit

    async def status(self, actor: Actor, connector_id: uuid.UUID) -> AuditStatus:
        connector = await self._connector(actor.scope, connector_id)
        async with self._store.begin(actor.scope) as transaction:
            chunking = await transaction.latest(connector_id, CHUNKING)
            embedding = await transaction.latest(connector_id, EMBEDDING)
        return AuditStatus(
            chunking=chunking,
            embedding=embedding,
            drift_estimate=await self._drift_estimate(connector, chunking),
        )

    async def find(self, actor: Actor, audit_id: uuid.UUID) -> IndexAudit:
        async with self._store.begin(actor.scope) as transaction:
            audit = await transaction.find(audit_id)
        if audit is None:
            raise NotFound("No such audit.")
        return audit

    async def alerts(self, actor: Actor) -> list[AuditAlert]:
        """The dashboard's degraded state: the newest audit per connector and kind whose
        worst finding is red. Connector names are looked up so the list reads."""
        async with self._store.begin(actor.scope) as transaction:
            latest = await transaction.latest_all()
        red = [audit for audit in latest if audit.status == SUCCEEDED and audit.severity == RED]
        if not red:
            return []
        names: dict[uuid.UUID, str] = {}
        async with self._connectors.begin(actor.scope) as connectors:
            for connector_id in {audit.connector_id for audit in red}:
                row = await connectors.connector(connector_id)
                if row is not None:
                    names[connector_id] = row.name
        return [
            AuditAlert(
                connector_id=audit.connector_id,
                connector_name=names.get(audit.connector_id),
                kind=audit.kind,
                audit_id=audit.id,
                finding=_worst_title(audit.report),
                created_at=audit.created_at,
            )
            for audit in red
            # A connector that has been deleted since has no name and no screen to open.
            if audit.connector_id in names
        ]

    # -- the job ------------------------------------------------------------

    async def run(self, organization_id: uuid.UUID, audit_id: uuid.UUID) -> None:
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            audit = await transaction.find(audit_id)
            if audit is None:
                logger.warning(
                    "audit row is gone; nothing to run", extra={"audit_id": str(audit_id)}
                )
                return
            if audit.status != RUNNING:
                return
            kind, connector_id, sample = audit.kind, audit.connector_id, audit.drift_sample

        try:
            connector = await self._connector(scope, connector_id)
            async with self._connectors.begin(scope) as connectors:
                rows = await connectors.audit_rows(connector_id)
            documents = [_record_of(row) for row in rows]
            if kind == CHUNKING:
                report, points = await self._chunking(connector, documents)
            else:
                report, points = await self._embedding(connector, documents, sample)
        except Exception as exc:
            logger.warning(
                "index audit failed",
                extra={"audit_id": str(audit_id), "kind": kind, "error": type(exc).__name__},
                exc_info=True,
            )
            await self._finish(scope, audit_id, status=FAILED, error=_describe(exc))
            return

        await self._finish(
            scope,
            audit_id,
            status=SUCCEEDED,
            report=report,
            points=points,
            severity=str(report.get("severity")),
        )
        logger.info(
            "index audit finished",
            extra={
                "audit_id": str(audit_id),
                "connector_id": str(connector_id),
                "kind": kind,
                "points": points,
                "severity": report.get("severity"),
                "findings": len(report.get("findings", [])),
            },
        )

    async def _chunking(
        self, connector: Connector, documents: Sequence[DocumentRecord]
    ) -> tuple[dict[str, Any], int]:
        points = [
            PointRecord.of(point.id, point.payload)
            async for point in self._scroll(connector, with_vectors=0)
        ]
        report = audit_chunking(points, documents, ChunkingConfig.load(connector.chunking))
        return report_json(report), report.points + report.summary_points

    async def _embedding(
        self,
        connector: Connector,
        documents: Sequence[DocumentRecord],
        drift_sample: int | None,
    ) -> tuple[dict[str, Any], int]:
        scanned: list[PointRecord] = []
        payloads: dict[str, Mapping[str, Any]] = {}
        total = 0
        async for point in self._scroll(connector, with_vectors=VECTOR_SCAN_LIMIT):
            total += 1
            if point.vector:
                scanned.append(PointRecord.of(point.id, point.payload, point.vector))
                payloads[point.id] = point.payload

        sources = [point for point in scanned if point.kind == KIND_SOURCE]
        neighbours = await self._neighbours(connector, sources)
        drift: list[DriftSample] = []
        if drift_sample:
            drift = await self._drift(sources, payloads, drift_sample)

        report = audit_embeddings(
            scanned,
            total_points=total,
            expected_dimension=self._embedder.dimension,
            expected_model=self._embedder.model,
            documents=documents,
            neighbours=neighbours,
            drift=drift,
        )
        return report_json(report), total

    async def _neighbours(
        self, connector: Connector, sources: Sequence[PointRecord]
    ) -> list[Neighbour]:
        """Intra-document agreement, asked of the index itself.

        One search per sampled chunk, against this connector only, over-fetching by two so
        that the chunk itself and a summary point can be skipped. A brute-force pass over
        the scanned vectors would answer the same question without the round trips, and
        would answer it about the *sample* rather than about the collection: a chunk whose
        true nearest neighbour was not among the five thousand scanned would look agreed
        with. The store's ranking is the one retrieval uses, which is the point.
        """
        per_document: dict[str, int] = {}
        for point in sources:
            per_document[point.document_id] = per_document.get(point.document_id, 0) + 1
        # Only chunks whose document has a second chunk can agree with it at all; a
        # single-chunk document's nearest neighbour is always elsewhere, and counting that
        # as disagreement would make every connector of short files look degenerate.
        eligible = [point for point in sources if per_document.get(point.document_id, 0) > 1]
        sample = _spread(eligible, AGREEMENT_SAMPLE)
        if not sample:
            return []
        store = await self._backends.store_for(connector.organization_id)
        found = []
        for point in sample:
            matches = await store.search(
                connector.organization_id,
                list(point.vector),
                connector_ids=[connector.id],
                limit=3,
                min_score=-1.0,
            )
            other = next(
                (
                    match
                    for match in matches
                    if match.id != point.id
                    and match.payload.get("kind", KIND_SOURCE) == KIND_SOURCE
                ),
                None,
            )
            found.append(
                Neighbour(
                    point_id=point.id,
                    document_id=point.document_id,
                    neighbour_id=other.id if other else None,
                    neighbour_document_id=(
                        str(other.payload.get("document_id")) if other else None
                    ),
                    score=other.score if other else None,
                )
            )
        return found

    async def _drift(
        self,
        sources: Sequence[PointRecord],
        payloads: Mapping[str, Mapping[str, Any]],
        sample_size: int,
    ) -> list[DriftSample]:
        """Re-embed a spread sample with the serving model and compare.

        The text embedded is :func:`embedding_input` of the payload — the window under
        ``sentence_window``, the prefixed text under ``contextual`` — because that is what
        ingestion embedded; re-embedding the chunk's display text would report drift on
        every windowed collection that has none.
        """
        sample = _spread(sources, min(sample_size, DRIFT_SAMPLE_MAX))
        budget = DRIFT_MAX_TOKENS
        chosen: list[PointRecord] = []
        for point in sample:
            if budget - point.token_count < 0 and chosen:
                break
            budget -= point.token_count
            chosen.append(point)
        if not chosen:
            return []
        fresh = await self._embedder.embed(
            [embedding_input(payloads[point.id]) for point in chosen]
        )
        return drift_cosines(chosen, fresh)

    async def _scroll(
        self, connector: Connector, *, with_vectors: int
    ) -> AsyncIterator[ChunkPoint]:
        """This connector's points out of the organization's live collection, one at a
        time. ``with_vectors`` is how many points to pull vectors for before switching
        to payload-only pages; zero for none."""
        organization_id = connector.organization_id
        index = await self._backends.admin_for(organization_id)
        live = await index.live_collection(organization_id)
        if live is None:
            return
        wanted = str(connector.id)
        cursor: str | None = None
        seen = 0
        while True:
            page = await index.scroll(
                live, cursor=cursor, limit=self._page, with_vectors=seen < with_vectors
            )
            for point in page.points:
                if str(point.payload.get("connector_id")) != wanted:
                    continue
                seen += 1
                yield point
            cursor = page.cursor
            if cursor is None or not page.points:
                return

    # -- internals ------------------------------------------------------------

    async def _connector(self, scope: TenantScope, connector_id: uuid.UUID) -> Connector:
        async with self._connectors.begin(scope) as transaction:
            connector = await transaction.connector(connector_id)
        if connector is None:
            raise NotFound(NO_SUCH_CONNECTOR)
        return connector

    async def _drift_estimate(
        self, connector: Connector, chunking: IndexAudit | None
    ) -> DriftEstimate:
        store = await self._backends.store_for(connector.organization_id)
        points = await store.count(connector.organization_id, connector_id=connector.id)
        sample = min(points, DRIFT_SAMPLE_DEFAULT)
        # The median chunk from the last chunking report, or the ceiling when there is
        # none: an upper bound rather than a guess.
        median = None
        if chunking is not None and chunking.status == SUCCEEDED:
            median = (chunking.report.get("distribution") or {}).get("median_tokens")
        per_chunk = int(median) if median else ChunkingConfig.load(connector.chunking).chunk_size
        return DriftEstimate(
            points=points, sample=sample, tokens=min(sample * per_chunk, DRIFT_MAX_TOKENS)
        )

    async def _finish(
        self,
        scope: TenantScope,
        audit_id: uuid.UUID,
        *,
        status: str,
        report: Mapping[str, Any] | None = None,
        points: int = 0,
        severity: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._store.begin(scope) as transaction:
            audit = await transaction.find(audit_id)
            if audit is None:
                return
            audit.status = status
            audit.report = dict(report or {})
            audit.points = points
            audit.severity = severity
            audit.error = error
            audit.finished_at = datetime.now(UTC)
            await transaction.commit()


def _record_of(row: DocumentAuditRow) -> DocumentRecord:
    return DocumentRecord(
        id=str(row.id),
        source_name=row.source_name,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        fingerprint=row.index_fingerprint,
        embedding_model=row.embedding_model,
    )


def _spread[T](items: Sequence[T], count: int) -> list[T]:
    """``count`` items spread evenly through the sequence. Deterministic, so two audits of
    an unchanged index sample the same chunks and their numbers can be compared."""
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


def _worst_title(report: Mapping[str, Any]) -> str:
    findings = report.get("findings") or []
    for finding in findings:
        if isinstance(finding, Mapping) and finding.get("severity") == RED:
            return str(finding.get("title", ""))
    return str(findings[0].get("title", "")) if findings else ""


def _describe(error: BaseException) -> str:
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


__all__ = [
    "AGREEMENT_SAMPLE",
    "DRIFT_MAX_TOKENS",
    "DRIFT_SAMPLE_DEFAULT",
    "DRIFT_SAMPLE_MAX",
    "VECTOR_SCAN_LIMIT",
    "AuditAlert",
    "AuditStatus",
    "DriftEstimate",
    "IndexAuditor",
]
