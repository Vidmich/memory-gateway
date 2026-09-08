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
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import Document
from app.db.models.connector import CONNECTOR_TYPES
from app.schemas.connector_config import ChunkingConfig
from app.services.connectors import (
    ConnectorDraft,
    ConnectorPatch,
    ConnectorView,
    PresignedUpload,
    UploadOutcome,
)
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
_NOT_NULLABLE = ("name", "chunking")


class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    description: Description | None = None
    type: str = "managed_file_drop"
    #: Partial. Anything omitted takes the SPEC §9.3 default, which the response then
    #: shows in full, so what is on screen is what will actually happen.
    chunking: dict[str, Any] = Field(default_factory=dict)

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
        )


class ConnectorUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: Description | None = None
    chunking: dict[str, Any] | None = None

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
        )


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
    #: True only in the response to the update that caused it. A permanent banner would
    #: be ignored within a day.
    reindex_required: bool
    last_synced_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, view: ConnectorView) -> ConnectorResponse:
        connector = view.connector
        return cls(
            id=connector.id,
            name=connector.name,
            description=connector.description,
            type=connector.type,
            status=connector.status,
            error=connector.error,
            storage_prefix=connector.storage_prefix,
            chunking=ChunkingConfig.load(connector.chunking),
            document_count=view.document_count,
            counts=dict(view.counts),
            total_bytes=view.total_bytes,
            reindex_required=view.reindex_required,
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
    content_hash: str | None
    indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, document: Document) -> DocumentResponse:
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
            content_hash=document.content_hash,
            indexed_at=document.indexed_at,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class DocumentChunk(BaseModel):
    """One indexed chunk, as the inspector shows it. No score: nothing was searched for."""

    id: str
    chunk_index: int | None
    page_or_section: str | None
    token_count: int | None
    text: str

    @classmethod
    def of(cls, chunk: Stored) -> DocumentChunk:
        payload = chunk.payload
        return cls(
            id=chunk.id,
            chunk_index=_number(payload.get("chunk_index")),
            page_or_section=_text(payload.get("page_or_section")),
            token_count=_number(payload.get("token_count")),
            text=chunk.text,
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


__all__ = [
    "ConnectorCreateRequest",
    "ConnectorResponse",
    "ConnectorUpdateRequest",
    "DocumentChunk",
    "DocumentChunksResponse",
    "DocumentResponse",
    "ResyncResponse",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "UploadOutcomeResponse",
    "UploadResponse",
    "UploadUrlRequest",
    "UploadUrlResponse",
]
