"""Request and response bodies for connectors and documents.

Two shapes are worth reading closely.

:class:`DocumentResponse` carries ``error`` on every row, including successful ones where
it is null. SPEC §13.1 asks for the extraction error *inline* in the document table, and a
detail endpoint per failed row would mean the table cannot show what is wrong until
somebody clicks — which is the opposite of the point.

:class:`ConnectorResponse` carries ``counts`` as an object keyed by status rather than a
handful of named integers. The statuses are :data:`DOCUMENT_STATUSES` and a column per
status is a schema change every time one is added; a map is one field, and the UI already
has to handle a status it does not recognise gracefully.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import Document
from app.db.models.connector import CONNECTOR_TYPES
from app.schemas.connector_config import ChunkingConfig, effective
from app.schemas.reprocessing import ReprocessingRunResponse
from app.schemas.summarization import SummarizationConfig
from app.schemas.summarization import effective as summarization_for
from app.services.chunking_preview import (
    MAX_CANDIDATES,
    CandidateResult,
    Distribution,
    PreviewChunk,
    PreviewResult,
    SummarizationCost,
)
from app.services.connectors import (
    ConnectorDraft,
    ConnectorPatch,
    ConnectorView,
    PresignedUpload,
    UploadOutcome,
)
from app.services.filetypes import FORMAT_KINDS
from app.services.index_fingerprint import Reason
from app.services.ingestion import ResyncSummary
from app.services.vector_store import Match, Stored

MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_FILENAME = 1024
MAX_QUERY = 2000

Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION)]

#: Fields that may be sent as ``null`` on a PATCH, meaning "clear it". Everything else
#: refuses ``null`` outright rather than ignoring it, so a caller who sends one is told.
_NOT_NULLABLE = ("name", "chunking", "summarization")


class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    description: Description | None = None
    type: str = "managed_file_drop"
    #: Partial. Anything omitted takes the SPEC §9.3 default, which the response then
    #: shows in full, so what is on screen is what will actually happen.
    chunking: dict[str, Any] = Field(default_factory=dict)
    #: Task 102. Partial like ``chunking``; off unless said otherwise.
    summarization: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_type(self) -> Self:
        if self.type not in CONNECTOR_TYPES:
            raise ValueError(f"must be one of {', '.join(CONNECTOR_TYPES)}")
        return self

    def to_draft(self) -> ConnectorDraft:
        return ConnectorDraft(
            name=self.name,
            description=self.description,
            type=self.type,
            chunking=self.chunking,
            summarization=self.summarization,
        )


class ConnectorUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: Description | None = None
    chunking: dict[str, Any] | None = None
    summarization: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in _NOT_NULLABLE:
                if field in data and data[field] is None:
                    raise ValueError(f"'{field}' cannot be null")
        return data

    def to_patch(self) -> ConnectorPatch:
        return ConnectorPatch(
            name=self.name,
            description=self.description,
            chunking=self.chunking,
            summarization=self.summarization,
        )


class SummaryModelResponse(BaseModel):
    """What a connector's summarization ``model_id`` resolves to (task 102)."""

    id: uuid.UUID
    name: str
    #: True when the connector named no model of its own and this came from further down
    #: the chain — the organization's default, its distillation model, or the platform's.
    inherited: bool


class ConnectorResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    type: str
    status: str
    error: str | None
    #: The key prefix objects live under. Shown because it is what a presigned upload or
    #: an ``aws s3 sync`` needs, and it is not a secret — knowing it grants nothing
    #: without a signature.
    storage_prefix: str | None
    chunking: ChunkingConfig
    document_count: int
    counts: dict[str, int]
    total_bytes: int
    #: What each format kind this connector could hold actually resolves to, once its
    #: override is applied. Sent rather than left to the client to recompute: the
    #: resolution rule lives in one place, and a screen that derived it independently
    #: would eventually show a configuration the pipeline does not use.
    effective_chunking: dict[str, ChunkingConfig]
    #: Task 102, the same two shapes as chunking: the stored settings and what each format
    #: resolves to under them.
    summarization: SummarizationConfig
    effective_summarization: dict[str, SummarizationConfig]
    #: The model the connector's summarization setting resolves to, or null when nothing
    #: is configured anywhere. Null on the list, where it is not read.
    summary_model: SummaryModelResponse | None
    #: Task 104: derived from the rows and persistent — true while any document is stale,
    #: on the PATCH that caused it and on every GET after. The banner it drives is on
    #: until the reprocess clears it, which is the point.
    reindex_required: bool
    #: The format kinds with stale documents, so the prompt can say "reprocess the 12
    #: code files" instead of "reprocess everything" when only an override moved.
    reindex_formats: list[str]
    #: Task 104. Documents by index status. ``stale_documents`` is what the list's badge
    #: and the header's sentence count; ``reprocessing_documents`` how many a run owns;
    #: ``unrecorded_documents`` how many indexed rows have no fingerprint to compare —
    #: shown as such, never as stale.
    stale_documents: int = 0
    reprocessing_documents: int = 0
    unrecorded_documents: int = 0
    #: What ingestion would write now for a document of each format — the thing every
    #: row's ``index_fingerprint`` is compared against. Empty on the list.
    effective_fingerprints: dict[str, str] = {}
    #: The reprocessing run in flight, or null.
    reprocessing: ReprocessingRunResponse | None = None
    last_synced_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, view: ConnectorView) -> ConnectorResponse:
        connector = view.connector
        chunking = ChunkingConfig.load(connector.chunking)
        summarization = SummarizationConfig.load(connector.summarization)
        return cls(
            id=connector.id,
            name=connector.name,
            description=connector.description,
            type=connector.type,
            status=connector.status,
            error=connector.error,
            storage_prefix=connector.storage_prefix,
            chunking=chunking,
            effective_chunking={kind: effective(chunking, kind) for kind in FORMAT_KINDS},
            summarization=summarization,
            effective_summarization={
                kind: summarization_for(summarization, kind) for kind in FORMAT_KINDS
            },
            summary_model=(
                SummaryModelResponse(
                    id=view.summary_model.id,
                    name=view.summary_model.name,
                    inherited=summarization.model_id is None
                    or view.summary_model.id != summarization.model_id,
                )
                if view.summary_model is not None
                else None
            ),
            document_count=view.document_count,
            counts=dict(view.counts),
            total_bytes=view.total_bytes,
            reindex_required=view.reindex_required,
            reindex_formats=sorted(view.reindex_formats),
            stale_documents=view.stale.stale,
            reprocessing_documents=view.stale.reprocessing,
            unrecorded_documents=view.stale.unrecorded,
            effective_fingerprints=dict(view.effective_fingerprints),
            reprocessing=(
                ReprocessingRunResponse.of(view.reprocessing)
                if view.reprocessing is not None
                else None
            ),
            last_synced_at=connector.last_synced_at,
            created_at=connector.created_at,
        )


class DocumentResponse(BaseModel):
    id: uuid.UUID
    connector_id: uuid.UUID
    source_name: str
    source_uri: str
    mime_type: str | None
    size_bytes: int
    status: str
    #: Why it failed or was skipped, in a sentence written for a customer. Null on a
    #: healthy row, and present on the list rather than behind a click.
    error: str | None
    #: The same fact as a stable code — ``needs_ocr``, ``password_protected``. The UI
    #: turns the ones it recognises into an explained state with a way out of it, and
    #: falls back to showing ``error`` for the ones it does not.
    reason: str | None
    chunk_count: int
    #: Pages, slides or sheets. Null where the format has no such unit.
    page_count: int | None
    embedding_model: str | None
    #: How this document was cut, which with per-format overrides is no longer answered by
    #: the connector's own setting. ``None`` for a document indexed before this was
    #: recorded — a blank rather than a guess, which is what makes it usable as drift.
    chunk_strategy: str | None
    #: What ``chunk_size`` was measured with when this document was cut (task 101):
    #: ``o200k_base``, ``approximate:3.6``, or ``words (cl100k_base unavailable)`` for a
    #: worker whose vocabulary failed to load. ``None`` for a row indexed before it was
    #: recorded, for the same reason as ``chunk_strategy``.
    tokenizer: str | None
    #: Task 104. The second status axis: ``current``, ``stale`` (cut under a previous
    #: configuration) or ``reprocessing`` (a run owns it). A document is ``indexed``
    #: and ``stale`` at once after a change, and that is the normal state.
    index_status: str = "current"
    #: ``stale`` as a boolean, for callers that read it before the axis existed.
    stale: bool = False
    #: Why the row is stale, as a code — ``chunking``, ``embedding_model``, ``tokenizer``,
    #: ``summarization``, ``extractor`` — or ``unrecorded`` for an indexed row with no
    #: fingerprint to compare, which is shown differently because it is not known to be
    #: wrong. Null on a current row.
    stale_reason: str | None = None
    #: The reason as a sentence naming the old and new values where the row kept them.
    stale_detail: str | None = None
    #: Everything the stored points depend on, as recorded when the row was indexed.
    index_fingerprint: str | None = None
    content_hash: str | None
    indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    #: Task 102. The summary and its provenance; ``summary_status`` is ``summarized``,
    #: ``failed`` or ``capped``, and null on a document nothing has tried to summarize.
    #: ``summary_model`` reads ``manual`` for one an operator wrote.
    summary: str | None = None
    summary_status: str | None = None
    summary_error: str | None = None
    summary_model: str | None = None
    summary_tokens_in: int | None = None
    summary_tokens_out: int | None = None
    summarized_at: datetime | None = None

    @classmethod
    def of(cls, document: Document, *, reason: Reason | None = None) -> DocumentResponse:
        return cls(
            id=document.id,
            connector_id=document.connector_id,
            source_name=document.source_name,
            source_uri=document.source_uri,
            mime_type=document.mime_type,
            size_bytes=document.size_bytes,
            status=document.status,
            error=document.error,
            reason=document.reason,
            chunk_count=document.chunk_count,
            page_count=document.page_count,
            embedding_model=document.embedding_model,
            chunk_strategy=document.chunk_strategy,
            tokenizer=document.tokenizer,
            index_status=document.index_status or "current",
            stale=document.index_status == "stale",
            stale_reason=reason.code if reason is not None else None,
            stale_detail=reason.sentence if reason is not None else None,
            index_fingerprint=document.index_fingerprint,
            content_hash=document.content_hash,
            indexed_at=document.indexed_at,
            created_at=document.created_at,
            updated_at=document.updated_at,
            summary=document.summary,
            summary_status=document.summary_status,
            summary_error=document.summary_error,
            summary_model=document.summary_model,
            summary_tokens_in=document.summary_tokens_in,
            summary_tokens_out=document.summary_tokens_out,
            summarized_at=document.summarized_at,
        )


class DocumentChunk(BaseModel):
    """One indexed chunk, as the inspector shows it. No score: nothing was searched for."""

    id: str
    chunk_index: int | None
    page_or_section: str | None
    token_count: int | None
    text: str
    #: The strategy this chunk was cut by, and — under ``sentence_window`` — the sentence
    #: inside ``text`` that was actually embedded. The inspector highlights it, and it has
    #: to: without it the first debugging session under that strategy is "why does this
    #: chunk not contain the words I searched for", and the answer is not discoverable
    #: from anything else on the screen.
    chunk_strategy: str | None = None
    embedded_text: str | None = None
    #: Task 102. ``source`` or ``summary`` — always one of the two, so the inspector can
    #: label the summary as what it is. ``context`` is the summary prefix this chunk was
    #: embedded behind under ``contextual``, and ``embedded_because`` says which of the
    #: two differences applies: ``window``, ``context`` or ``window+context``.
    kind: str = "source"
    context: str | None = None
    embedded_because: str | None = None

    @classmethod
    def of(cls, chunk: Stored) -> DocumentChunk:
        payload = chunk.payload
        embedded = _text(payload.get("embedded_text"))
        return cls(
            id=chunk.id,
            chunk_index=_number(payload.get("chunk_index")),
            page_or_section=_text(payload.get("page_or_section")),
            token_count=_number(payload.get("token_count")),
            text=chunk.text,
            chunk_strategy=_text(payload.get("chunk_strategy")),
            embedded_text=embedded if embedded and embedded != chunk.text else None,
            kind="summary" if payload.get("kind") == "summary" else "source",
            context=_text(payload.get("context")),
            embedded_because=_text(payload.get("embedded_because")),
        )


class DocumentChunksResponse(BaseModel):
    chunks: list[DocumentChunk]
    #: What the row says it has. Shown beside the number returned, because the two
    #: disagreeing is itself the finding: a document that reports twelve chunks and has
    #: three in the index was reindexed into a collection that has since been dropped.
    chunk_count: int


class UploadOutcomeResponse(BaseModel):
    """One file's fate. A batch returns one of these per file, always 200.

    A rejected file is not an HTTP error, because the other thirty-nine in the same
    request were fine. The status code answers "did the request work"; this answers "what
    happened to each file", and conflating them makes a partial success unreportable.
    """

    filename: str
    document_id: uuid.UUID | None
    status: str
    error: str | None

    @classmethod
    def of(cls, outcome: UploadOutcome) -> UploadOutcomeResponse:
        return cls(
            filename=outcome.filename,
            document_id=outcome.document_id,
            status=outcome.status,
            error=outcome.error,
        )


class UploadResponse(BaseModel):
    files: list[UploadOutcomeResponse]

    @property
    def accepted(self) -> int:
        return sum(1 for file in self.files if file.document_id is not None)


class UploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: Annotated[str, Field(min_length=1, max_length=MAX_FILENAME)]


class UploadUrlResponse(BaseModel):
    url: str
    key: str
    expires_in: int

    @classmethod
    def of(cls, presigned: PresignedUpload) -> UploadUrlResponse:
        return cls(url=presigned.url, key=presigned.key, expires_in=presigned.expires_in)


class ResyncResponse(BaseModel):
    """SPEC §9.1's reconciliation summary."""

    added: int
    updated: int
    deleted: int
    unchanged: int
    skipped: int

    @classmethod
    def of(cls, summary: ResyncSummary) -> ResyncResponse:
        return cls(
            added=summary.added,
            updated=summary.updated,
            deleted=summary.deleted,
            unchanged=summary.unchanged,
            skipped=summary.skipped,
        )


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY)]
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class SearchHit(BaseModel):
    """One chunk, with everything needed to judge whether retrieval is working: the score,
    the text, and where in which file it came from."""

    id: str
    score: float
    text: str
    source_name: str | None
    source_uri: str | None
    page_or_section: str | None
    chunk_index: int | None
    document_id: uuid.UUID | None

    @classmethod
    def of(cls, match: Match) -> SearchHit:
        payload = match.payload
        return cls(
            id=match.id,
            score=match.score,
            text=match.text,
            source_name=_text(payload.get("source_name")),
            source_uri=_text(payload.get("source_uri")),
            page_or_section=_text(payload.get("page_or_section")),
            chunk_index=_number(payload.get("chunk_index")),
            document_id=_identifier(payload.get("document_id")),
        )


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    #: Which model produced the query vector. Two runs with different models are not
    #: comparable, and this is the only place the difference is visible.
    embedding_model: str


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _identifier(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        # A payload written by a different build, or by hand. A hit that cannot name its
        # document is still a useful hit.
        return None


class ReindexRequest(BaseModel):
    """Which formats to re-run. Absent means every document."""

    model_config = ConfigDict(extra="forbid")

    #: Format kinds, as reported by ``ConnectorResponse.reindex_formats``. Narrowing to
    #: them is what makes a per-format override affordable: adding one for code re-runs
    #: the code files and leaves a thousand PDFs where they are.
    formats: list[str] | None = None

    @model_validator(mode="after")
    def _known_formats(self) -> Self:
        unknown = sorted(set(self.formats or ()) - set(FORMAT_KINDS))
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)}: not a format this build classifies. "
                f"Available: {', '.join(FORMAT_KINDS)}."
            )
        return self


class ChunkingCandidateRequest(BaseModel):
    """One configuration to compare, as a partial.

    Partial rather than whole, so "the same but semantic" is one key. Anything omitted is
    taken from the connector's *effective* configuration for this document's format, which
    is the only baseline a comparison against this document means anything against.
    """

    model_config = ConfigDict(extra="allow")

    label: str | None = Field(default=None, max_length=60)


class ChunkingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    candidates: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    #: Optional. With one, each candidate also reports the chunk it would surface — which
    #: is the question anybody comparing chunkings is actually asking.
    query: str | None = Field(default=None, max_length=MAX_QUERY)


class ChunkDistribution(BaseModel):
    """Four numbers and two counts. Enough to compare two strategies, short enough to read
    at a glance — which a wall of chunk text is not."""

    chunks: int
    min_tokens: int
    median_tokens: int
    p95_tokens: int
    max_tokens: int
    at_ceiling: int
    mid_sentence: int

    @classmethod
    def of(cls, found: Distribution) -> ChunkDistribution:
        return cls(**asdict(found))


class PreviewChunkResponse(BaseModel):
    index: int
    text: str
    section: str | None
    token_count: int
    embedded_text: str | None
    score: float | None
    #: Task 102: the summary prefix this chunk would be embedded behind, under
    #: ``contextual``. The same string ingestion would use.
    context: str | None = None

    @classmethod
    def of(cls, chunk: PreviewChunk) -> PreviewChunkResponse:
        return cls(
            index=chunk.index,
            text=chunk.text,
            section=chunk.section,
            token_count=chunk.token_count,
            embedded_text=chunk.embedded_text,
            score=chunk.score,
            context=chunk.context,
        )


class SummarizationCostResponse(BaseModel):
    """The summarization call's share of one ingestion, on the comparison (task 102)."""

    mode: str
    tokens_in: int
    tokens_out: int
    summary: str | None
    prefixes: bool

    @classmethod
    def of(cls, cost: SummarizationCost) -> SummarizationCostResponse:
        return cls(
            mode=cost.mode,
            tokens_in=cost.tokens_in,
            tokens_out=cost.tokens_out,
            summary=cost.summary,
            prefixes=cost.prefixes,
        )


class ChunkingCandidateResponse(BaseModel):
    label: str
    strategy: str
    distribution: ChunkDistribution
    chunks: list[PreviewChunkResponse]
    total_chunks: int
    #: What one ingestion of this document costs at the embedding provider under this
    #: candidate. Reported beside the quality numbers on purpose: a comparison that showed
    #: quality and hid cost would push every reader toward the most expensive option.
    embedded_texts: int
    best: int | None

    @classmethod
    def of(cls, result: CandidateResult) -> ChunkingCandidateResponse:
        return cls(
            label=result.label,
            strategy=result.strategy,
            distribution=ChunkDistribution.of(result.distribution),
            chunks=[PreviewChunkResponse.of(chunk) for chunk in result.chunks],
            total_chunks=result.total_chunks,
            embedded_texts=result.embedded_texts,
            best=result.best,
        )


class ChunkingPreviewResponse(BaseModel):
    document_id: uuid.UUID
    source_name: str
    media_type: str | None
    format_kind: str
    query: str | None
    candidates: list[ChunkingCandidateResponse]
    #: Null when the connector does not summarize this document's format.
    summarization: SummarizationCostResponse | None = None

    @classmethod
    def of(cls, result: PreviewResult) -> ChunkingPreviewResponse:
        return cls(
            document_id=result.document_id,
            source_name=result.source_name,
            media_type=result.media_type,
            format_kind=result.format_kind,
            query=result.query,
            candidates=[ChunkingCandidateResponse.of(one) for one in result.candidates],
            summarization=(
                SummarizationCostResponse.of(result.summarization)
                if result.summarization is not None
                else None
            ),
        )


class ReindexSummary(BaseModel):
    """How many documents a connector-wide reindex put back in the queue.

    A count rather than a list: what the person pressing the button needs to know is
    whether it did anything and roughly how long to wait, and the document table beside it
    is already about to show every one of them turn ``pending``.
    """

    documents: int = 0


__all__ = [
    "ChunkDistribution",
    "ChunkingCandidateRequest",
    "ChunkingCandidateResponse",
    "ChunkingPreviewRequest",
    "ChunkingPreviewResponse",
    "ConnectorCreateRequest",
    "ConnectorResponse",
    "ConnectorUpdateRequest",
    "DocumentChunk",
    "DocumentChunksResponse",
    "DocumentResponse",
    "PreviewChunkResponse",
    "ReindexRequest",
    "ReindexSummary",
    "ResyncResponse",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "SummarizationCostResponse",
    "SummaryModelResponse",
    "UploadOutcomeResponse",
    "UploadResponse",
    "UploadUrlRequest",
    "UploadUrlResponse",
]
