"""Request and response bodies for the validation screens (task 103, SPEC §6.6).

Two families. The **audit** responses carry a report the worker stored as JSONB and typed
here for the client: the chunking report's histogram, distribution and findings, the
embedding report's checks. The **evaluation** bodies are sets, items and runs — and a run's
per-item results ride on the run row as the worker wrote them, typed here the same way.

An item's label in a request is the minimum a person or Try retrieval has in hand: a chunk
id and its document. The service fills in the text and the name from the index, because
the text is what makes the label survive a reindex and the client should not be trusted to
copy it faithfully.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.validation import MAX_QUESTION_LENGTH, MAX_SET_NAME_LENGTH

if TYPE_CHECKING:
    from app.db.models import EvaluationItem, EvaluationRun, EvaluationSet, IndexAudit
    from app.services.evaluation_service import (
        GenerateResult,
        ImportResult,
        RunDiff,
        SetDetail,
        SetView,
    )
    from app.services.evaluation_store import ItemCounts
    from app.services.index_auditor import AuditAlert, AuditStatus


# ---------------------------------------------------------------------------
# audits
# ---------------------------------------------------------------------------

AuditKind = Literal["chunking", "embedding"]
Severity = Literal["green", "amber", "red"]


class FindingDocumentResponse(BaseModel):
    id: str
    source_name: str
    count: int


class FindingResponse(BaseModel):
    code: str
    severity: Severity
    title: str
    count: int
    detail: str
    documents: list[FindingDocumentResponse] = Field(default_factory=list)
    document_count: int = 0
    action: Literal["compare", "reindex", "platform", "none"] = "none"


class DistributionResponse(BaseModel):
    chunks: int
    min_tokens: int
    median_tokens: int
    p95_tokens: int
    max_tokens: int
    at_ceiling: int
    mid_sentence: int


class HistogramBucketResponse(BaseModel):
    lower: int
    upper: int
    count: int


class HistogramResponse(BaseModel):
    bucket_tokens: int
    buckets: list[HistogramBucketResponse]


class FormatReportResponse(BaseModel):
    kind: str
    points: int
    documents: int
    chunk_size: int
    distribution: DistributionResponse
    histogram: HistogramResponse
    findings: list[FindingResponse]


class ChunkingReportResponse(BaseModel):
    kind: Literal["chunking"] = "chunking"
    points: int
    summary_points: int = 0
    documents: int
    documents_without_points: int = 0
    chunk_size: int
    distribution: DistributionResponse
    histogram: HistogramResponse
    formats: list[FormatReportResponse]
    findings: list[FindingResponse]
    fingerprints: dict[str, int] = Field(default_factory=dict)
    severity: Severity = "green"


class NormsResponse(BaseModel):
    min: float
    median: float
    p95: float
    max: float


class AgreementResponse(BaseModel):
    sampled: int
    agreed: int
    rate: float | None
    cross_document_similarity: float | None
    worst: list[FindingDocumentResponse]


class DriftResponse(BaseModel):
    sampled: int
    mean: float
    min: float
    p5: float
    below: float
    shape: Literal["healthy", "bimodal", "offset", "low"]
    model: str


class EmbeddingReportResponse(BaseModel):
    kind: Literal["embedding"] = "embedding"
    points: int
    scanned: int
    expected_dimension: int
    dimensions: dict[str, int]
    expected_model: str
    document_models: dict[str, int]
    norms: NormsResponse | None
    zero_vectors: int
    identical_vectors: int
    agreement: AgreementResponse | None
    drift: DriftResponse | None
    findings: list[FindingResponse]
    severity: Severity = "green"


class AuditResponse(BaseModel):
    id: uuid.UUID
    connector_id: uuid.UUID
    kind: AuditKind
    status: Literal["running", "succeeded", "failed"]
    created_at: datetime
    finished_at: datetime | None
    points: int
    drift_sample: int | None
    severity: Severity | None
    error: str | None
    report: ChunkingReportResponse | EmbeddingReportResponse | None

    @classmethod
    def of(cls, audit: IndexAudit) -> AuditResponse:
        report: ChunkingReportResponse | EmbeddingReportResponse | None = None
        if audit.status == "succeeded" and audit.report:
            if audit.kind == "chunking":
                report = ChunkingReportResponse.model_validate(audit.report)
            else:
                report = EmbeddingReportResponse.model_validate(audit.report)
        return cls(
            id=audit.id,
            connector_id=audit.connector_id,
            kind=audit.kind,  # type: ignore[arg-type]
            status=audit.status,  # type: ignore[arg-type]
            created_at=audit.created_at,
            finished_at=audit.finished_at,
            points=audit.points,
            drift_sample=audit.drift_sample,
            severity=audit.severity,  # type: ignore[arg-type]
            error=audit.error,
            report=report,
        )


class DriftEstimateResponse(BaseModel):
    points: int
    sample: int
    tokens: int


class AuditStatusResponse(BaseModel):
    """The connector's Validation section: the latest of each kind, and the price of the
    one check that spends."""

    chunking: AuditResponse | None
    embedding: AuditResponse | None
    drift_estimate: DriftEstimateResponse

    @classmethod
    def of(cls, status: AuditStatus) -> AuditStatusResponse:
        return cls(
            chunking=AuditResponse.of(status.chunking) if status.chunking else None,
            embedding=AuditResponse.of(status.embedding) if status.embedding else None,
            drift_estimate=DriftEstimateResponse(
                points=status.drift_estimate.points,
                sample=status.drift_estimate.sample,
                tokens=status.drift_estimate.tokens,
            ),
        )


class AuditRequest(BaseModel):
    """``drift_sample`` is the embedding audit's one spending option: how many chunks to
    re-embed with the current model. Omit it and the audit reads only."""

    model_config = ConfigDict(extra="forbid")

    drift_sample: int | None = Field(default=None, ge=1, le=1000)


class AuditAlertResponse(BaseModel):
    connector_id: uuid.UUID
    connector_name: str | None
    kind: AuditKind
    audit_id: uuid.UUID
    finding: str
    created_at: datetime

    @classmethod
    def of(cls, alert: AuditAlert) -> AuditAlertResponse:
        return cls(
            connector_id=alert.connector_id,
            connector_name=alert.connector_name,
            kind=alert.kind,  # type: ignore[arg-type]
            audit_id=alert.audit_id,
            finding=alert.finding,
            created_at=alert.created_at,
        )


class AuditAlertList(BaseModel):
    items: list[AuditAlertResponse]


# ---------------------------------------------------------------------------
# evaluation sets and items
# ---------------------------------------------------------------------------

ItemSource = Literal["log", "citation", "manual", "generated"]


class LabelRequest(BaseModel):
    """A chunk somebody says answers the question. The text is filled in from the index."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=64)
    document_id: uuid.UUID


class LabelResponse(BaseModel):
    chunk_id: str
    document_id: str | None
    source_name: str | None
    text: str | None


class ItemCountsResponse(BaseModel):
    total: int = 0
    verified: int = 0
    generated: int = 0
    negatives: int = 0

    @classmethod
    def of(cls, counts: ItemCounts) -> ItemCountsResponse:
        return cls(
            total=counts.total,
            verified=counts.verified,
            generated=counts.generated,
            negatives=counts.negatives,
        )


class EvaluationItemResponse(BaseModel):
    id: uuid.UUID
    set_id: uuid.UUID
    question: str
    relevant: list[LabelResponse]
    relevant_document_ids: list[str]
    source: ItemSource
    verified: bool
    #: No label at all: a question the corpus should answer with nothing.
    negative: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, item: EvaluationItem) -> EvaluationItemResponse:
        labels = [
            LabelResponse(
                chunk_id=str(entry.get("chunk_id", "")),
                document_id=_optional(entry.get("document_id")),
                source_name=_optional(entry.get("source_name")),
                text=_optional(entry.get("text")),
            )
            for entry in (item.relevant or [])
            if isinstance(entry, dict) and entry.get("chunk_id")
        ]
        documents = [str(value) for value in (item.relevant_document_ids or [])]
        return cls(
            id=item.id,
            set_id=item.set_id,
            question=item.question,
            relevant=labels,
            relevant_document_ids=documents,
            source=item.source,  # type: ignore[arg-type]
            verified=item.verified,
            negative=not labels and not documents,
            notes=item.notes,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class EvaluationItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    relevant: list[LabelRequest] = Field(default_factory=list, max_length=50)
    relevant_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    #: A person adding an item has checked it; ``manual`` is verified unless said otherwise.
    verified: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class EvaluationItemPatch(BaseModel):
    """Every field optional; ``relevant: []`` with ``relevant_document_ids: []`` makes the
    item a negative."""

    model_config = ConfigDict(extra="forbid")

    question: str | None = Field(default=None, min_length=1, max_length=MAX_QUESTION_LENGTH)
    relevant: list[LabelRequest] | None = Field(default=None, max_length=50)
    relevant_document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    verified: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    def patch(self) -> dict[str, Any]:
        """The fields that were sent. ``notes: null`` clears the notes; a null anywhere
        else is not a value and is dropped."""
        values = self.model_dump(exclude_unset=True)
        patch = {key: value for key, value in values.items() if value is not None or key == "notes"}
        if patch.get("relevant") is not None:
            patch["relevant"] = [
                {"chunk_id": label["chunk_id"], "document_id": str(label["document_id"])}
                for label in patch["relevant"]
            ]
        if patch.get("relevant_document_ids") is not None:
            patch["relevant_document_ids"] = [str(v) for v in patch["relevant_document_ids"]]
        return patch


class EvaluationRunSummaryResponse(BaseModel):
    """A run as the history table shows it: status, progress, the headline numbers."""

    id: uuid.UUID
    set_id: uuid.UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    total_items: int
    completed_items: int
    #: The unsaved patch the run applied, or ``None`` for the saved gateway.
    patch: dict[str, Any] | None
    metrics: dict[str, Any]
    snapshot: dict[str, Any]
    config: dict[str, Any]
    error: str | None

    @classmethod
    def of(cls, run: EvaluationRun) -> EvaluationRunSummaryResponse:
        return cls(
            id=run.id,
            set_id=run.set_id,
            status=run.status,  # type: ignore[arg-type]
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            total_items=run.total_items,
            completed_items=run.completed_items,
            patch=dict(run.patch) if run.patch else None,
            metrics=dict(run.metrics or {}),
            snapshot=dict(run.snapshot or {}),
            config=dict(run.config or {}),
            error=run.error,
        )


class EvaluationRunResponse(EvaluationRunSummaryResponse):
    """The whole run: the summary plus every item's result."""

    results: list[dict[str, Any]]

    @classmethod
    def of(cls, run: EvaluationRun) -> EvaluationRunResponse:
        summary = EvaluationRunSummaryResponse.of(run)
        return cls(**summary.model_dump(), results=[dict(r) for r in (run.results or [])])


class EvaluationSetResponse(BaseModel):
    id: uuid.UUID
    gateway_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    counts: ItemCountsResponse
    last_run: EvaluationRunSummaryResponse | None = None

    @classmethod
    def of(cls, view: SetView) -> EvaluationSetResponse:
        return cls(
            **_set_fields(view.set),
            counts=ItemCountsResponse.of(view.counts),
            last_run=EvaluationRunSummaryResponse.of(view.last_run) if view.last_run else None,
        )


class EvaluationSetDetailResponse(BaseModel):
    id: uuid.UUID
    gateway_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    counts: ItemCountsResponse
    items: list[EvaluationItemResponse]

    @classmethod
    def of(cls, detail: SetDetail) -> EvaluationSetDetailResponse:
        return cls(
            **_set_fields(detail.set),
            counts=ItemCountsResponse.of(detail.counts),
            items=[EvaluationItemResponse.of(item) for item in detail.items],
        )


class EvaluationSetList(BaseModel):
    items: list[EvaluationSetResponse]


class EvaluationSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_SET_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=2000)


class EvaluationSetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_SET_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=2000)


class ImportRequest(BaseModel):
    """A window of the gateway's own log. ``uncited`` narrows it the way the monitoring
    filter does: ``true`` for requests whose answer cited nothing, ``false`` for the ones
    that did, omitted for both."""

    model_config = ConfigDict(extra="forbid")

    start: datetime = Field(alias="from")
    end: datetime = Field(alias="to")
    uncited: bool | None = None
    limit: int = Field(default=200, ge=1, le=500)


class ImportResponse(BaseModel):
    imported: int
    duplicates: int
    skipped: int
    labelled: int

    @classmethod
    def of(cls, result: ImportResult) -> ImportResponse:
        return cls(
            imported=result.imported,
            duplicates=result.duplicates,
            skipped=result.skipped,
            labelled=result.labelled,
        )


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=10, ge=1, le=25)


class GenerateResponse(BaseModel):
    generated: int
    failed: int
    tokens_in: int
    tokens_out: int
    model_name: str | None

    @classmethod
    def of(cls, result: GenerateResult) -> GenerateResponse:
        return cls(
            generated=result.generated,
            failed=result.failed,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model_name=result.model_name,
        )


class RunRequest(BaseModel):
    """``memory_config`` is the editor's unsaved form, merged the way Try retrieval merges
    it; omit it to run the saved gateway."""

    model_config = ConfigDict(extra="forbid")

    memory_config: dict[str, Any] | None = None


class EvaluationRunList(BaseModel):
    items: list[EvaluationRunSummaryResponse]


class MetricDeltaResponse(BaseModel):
    name: str
    before: float | None
    after: float | None
    change: float | None


class RunDiffResponse(BaseModel):
    before: EvaluationRunSummaryResponse
    after: EvaluationRunSummaryResponse
    metrics: list[MetricDeltaResponse]
    config_changes: dict[str, list[Any]]
    index_changes: list[str]
    won: list[dict[str, Any]]
    lost: list[dict[str, Any]]

    @classmethod
    def of(cls, diff: RunDiff) -> RunDiffResponse:
        return cls(
            before=EvaluationRunSummaryResponse.of(diff.before),
            after=EvaluationRunSummaryResponse.of(diff.after),
            metrics=[
                MetricDeltaResponse(
                    name=delta.name, before=delta.before, after=delta.after, change=delta.change
                )
                for delta in diff.metrics
            ],
            config_changes={key: list(value) for key, value in diff.config_changes.items()},
            index_changes=list(diff.index_changes),
            won=[dict(entry) for entry in diff.won],
            lost=[dict(entry) for entry in diff.lost],
        )


def _set_fields(row: EvaluationSet) -> dict[str, Any]:
    return {
        "id": row.id,
        "gateway_id": row.gateway_id,
        "name": row.name,
        "description": row.description,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _optional(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


__all__ = [
    "AuditAlertList",
    "AuditAlertResponse",
    "AuditRequest",
    "AuditResponse",
    "AuditStatusResponse",
    "ChunkingReportResponse",
    "EmbeddingReportResponse",
    "EvaluationItemPatch",
    "EvaluationItemRequest",
    "EvaluationItemResponse",
    "EvaluationRunList",
    "EvaluationRunResponse",
    "EvaluationRunSummaryResponse",
    "EvaluationSetDetailResponse",
    "EvaluationSetList",
    "EvaluationSetPatch",
    "EvaluationSetRequest",
    "EvaluationSetResponse",
    "FindingResponse",
    "GenerateRequest",
    "GenerateResponse",
    "ImportRequest",
    "ImportResponse",
    "LabelRequest",
    "RunDiffResponse",
    "RunRequest",
]
