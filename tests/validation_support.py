"""Task 103's stack over memory: the auditor, the evaluation runner and service, wired to a
:class:`~tests.connector_support.ConnectorFixture`'s index, rows and queue.

Built *onto* the connector fixture rather than beside it, so an audit scrolls the collection
an upload in the same test actually built, and an evaluation run retrieves through the same
:class:`~app.services.retrieval.MemoryService` the fixture's Try retrieval uses. The
connector fixture's job runner is replaced with one that also knows the two new jobs, so
``run_jobs()`` drains audits and runs the way a worker would.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.ids import uuid7
from app.db.models import EvaluationItem, EvaluationRun, EvaluationSet, Gateway, IndexAudit
from app.services.evaluation_runner import EvaluationRunner
from app.services.evaluation_service import EvaluationService, QuestionWriter
from app.services.evaluation_store import MemoryEvaluationStore
from app.services.fact_vectors import MemoryFactVectorStore
from app.services.gateway_store import MemoryGatewayStore
from app.services.index_audit_store import MemoryIndexAuditStore
from app.services.index_auditor import IndexAuditor
from app.services.jobs import (
    AUDIT_INDEX,
    DELETE_CONNECTOR,
    EVALUATE_SET,
    INGEST_DOCUMENT,
    SUMMARIZE_DOCUMENT,
    JobRunner,
    RetryPolicy,
)
from app.services.memory_preview import MemoryPreview
from app.services.metrics_store import MemoryMetricsRepository
from app.services.retrieval import MemoryService
from app.services.vector_backends import Backend, VectorBackends
from app.services.vector_binding_store import MemoryVectorBindingStore
from app.services.vector_index import MemoryVectorIndexAdmin
from tests.connector_support import TOKENIZER, ConnectorFixture, ScriptedSummaryModel


@dataclass
class ValidationFixture:
    connectors: ConnectorFixture
    audits: MemoryIndexAuditStore
    evaluations: MemoryEvaluationStore
    backends: VectorBackends
    gateways: MemoryGatewayStore
    auditor: IndexAuditor
    runner: EvaluationRunner
    service: EvaluationService
    #: The same retriever the runner scores with, wrapped the way the editor wraps it — so
    #: a parity test compares the two through their real entry points.
    preview: MemoryPreview
    memory: MemoryService
    question_model: ScriptedSummaryModel

    @property
    def organization_id(self) -> uuid.UUID:
        return self.connectors.organization_id

    async def run_jobs(self) -> int:
        return await self.connectors.run_jobs()


def build_validation_fixture(
    connectors: ConnectorFixture,
    *,
    memory: MemoryService | None = None,
    gateways: MemoryGatewayStore | None = None,
    question_model: ScriptedSummaryModel | None = None,
) -> ValidationFixture:
    database = connectors.database
    vectors = connectors.vectors
    backends = VectorBackends(
        {
            "qdrant": Backend(
                kind="qdrant",
                store=vectors,
                facts=MemoryFactVectorStore(),
                admin=MemoryVectorIndexAdmin(vectors),
            )
        },
        MemoryVectorBindingStore(),
        default="qdrant",
    )
    gateway_store = gateways or MemoryGatewayStore(database)
    retrieval = memory or connectors.memory
    audits = MemoryIndexAuditStore(database)
    evaluations = MemoryEvaluationStore(database)
    auditor = IndexAuditor(
        audits,
        connectors=connectors.store,
        backends=backends,
        embedder=connectors.embedder,
        queue=connectors.queue,
    )
    runner = EvaluationRunner(
        evaluations,
        gateways=gateway_store,
        connectors=connectors.store,
        memory=retrieval,
        vectors=vectors,
        embedder=connectors.embedder,
        tokenizer=TOKENIZER,
        progress_every=2,
    )
    scripted = question_model or ScriptedSummaryModel()
    service = EvaluationService(
        evaluations,
        gateways=gateway_store,
        connectors=connectors.store,
        vectors=vectors,
        logs=MemoryMetricsRepository(database),
        queue=connectors.queue,
        writer=QuestionWriter(
            models=connectors.summary_models, proxy=scripted, ledger=connectors.summaries
        ),
    )

    pipeline = connectors.pipeline

    async def ingest(payload: Mapping[str, Any]) -> None:
        run_id = payload.get("run_id")
        await pipeline.ingest(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            document_id=uuid.UUID(str(payload["document_id"])),
            run_id=uuid.UUID(str(run_id)) if run_id else None,
        )

    async def purge(payload: Mapping[str, Any]) -> None:
        await pipeline.purge(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            connector_id=uuid.UUID(str(payload["connector_id"])),
        )

    async def summarize(payload: Mapping[str, Any]) -> None:
        await pipeline.summarize(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            document_id=uuid.UUID(str(payload["document_id"])),
            regenerate=bool(payload.get("regenerate", False)),
        )

    async def audit(payload: Mapping[str, Any]) -> None:
        await auditor.run(
            uuid.UUID(str(payload["organization_id"])), uuid.UUID(str(payload["audit_id"]))
        )

    async def evaluate(payload: Mapping[str, Any]) -> None:
        await runner.run(
            uuid.UUID(str(payload["organization_id"])), uuid.UUID(str(payload["run_id"]))
        )

    connectors.runner = JobRunner(
        {
            INGEST_DOCUMENT: ingest,
            DELETE_CONNECTOR: purge,
            SUMMARIZE_DOCUMENT: summarize,
            AUDIT_INDEX: audit,
            EVALUATE_SET: evaluate,
        },
        queue=connectors.queue,
        dead_letters=connectors.dead_letters,
        policy=RetryPolicy(jitter=lambda _, ceiling: ceiling),
    )

    return ValidationFixture(
        connectors=connectors,
        audits=audits,
        evaluations=evaluations,
        backends=backends,
        gateways=gateway_store,
        auditor=auditor,
        runner=runner,
        service=service,
        preview=MemoryPreview(gateway_store, memory=retrieval, tokenizer=TOKENIZER),
        memory=retrieval,
        question_model=scripted,
    )


def make_evaluation_set(gateway: Gateway, *, name: str = "Regression") -> EvaluationSet:
    now = datetime.now(UTC)
    return EvaluationSet(
        id=uuid7(),
        organization_id=gateway.organization_id,
        gateway_id=gateway.id,
        name=name,
        description=None,
        created_by=None,
        created_at=now,
        updated_at=now,
    )


def make_evaluation_item(
    evaluation_set: EvaluationSet,
    *,
    question: str = "What is the refund policy?",
    relevant: list[dict[str, Any]] | None = None,
    relevant_document_ids: list[str] | None = None,
    source: str = "manual",
    verified: bool = True,
) -> EvaluationItem:
    now = datetime.now(UTC)
    return EvaluationItem(
        id=uuid7(),
        organization_id=evaluation_set.organization_id,
        set_id=evaluation_set.id,
        question=question,
        relevant=list(relevant or []),
        relevant_document_ids=list(relevant_document_ids or []),
        source=source,
        verified=verified,
        notes=None,
        created_at=now,
        updated_at=now,
    )


def make_evaluation_run(
    evaluation_set: EvaluationSet, *, status: str = "succeeded", **overrides: Any
) -> EvaluationRun:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": evaluation_set.organization_id,
        "set_id": evaluation_set.id,
        "gateway_id": evaluation_set.gateway_id,
        "status": status,
        "created_by": None,
        "patch": None,
        "config": {},
        "snapshot": {},
        "metrics": {},
        "results": [],
        "total_items": 0,
        "completed_items": 0,
        "error": None,
        "created_at": now,
        "started_at": now if status != "queued" else None,
        "finished_at": now if status in ("succeeded", "failed") else None,
    }
    values.update(overrides)
    return EvaluationRun(**values)


def make_index_audit(
    connector_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    kind: str = "chunking",
    status: str = "succeeded",
    severity: str | None = "green",
    report: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> IndexAudit:
    when = created_at or datetime.now(UTC)
    return IndexAudit(
        id=uuid7(),
        organization_id=organization_id,
        connector_id=connector_id,
        kind=kind,
        status=status,
        created_by=None,
        drift_sample=None,
        points=0,
        report=dict(report or {}),
        severity=severity,
        error=None,
        created_at=when,
        finished_at=when if status != "running" else None,
    )


__all__ = [
    "ValidationFixture",
    "build_validation_fixture",
    "make_evaluation_item",
    "make_evaluation_run",
    "make_evaluation_set",
    "make_index_audit",
]
