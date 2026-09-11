"""``/api/v1/connectors`` — content sources and the documents they hold.

Same three-line shape as the other control-plane routers: name a capability, take
:data:`CurrentActor`, hand both to the service. No route takes an ``organization_id`` —
which organization a connector belongs to is decided by the scope on the way in and read
off the row on the way out.

Two routes are shaped differently from anything before, and both for the same reason: they
are about *files* rather than JSON.

``POST /connectors/{id}/upload`` returns **200 with a per-file outcome list**, never a 4xx
for a rejected file. Somebody drags in a folder; one file is a 2 GB video and the other
thirty-nine are fine. A 413 for the batch would throw away the thirty-nine, and there is
no status code that means "mostly worked". The HTTP status answers "did the request
work"; the body answers "what happened to each file".

``POST /connectors/{id}/search`` is a **debug** endpoint, and is labelled one. It exists to
prove the index works before task 10's retrieval does, and it stays afterwards because
"is this chunk actually in there" is the first question anybody asks when a gateway's
answers look wrong.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.api.control.deps import CurrentActor, get_connector_service, require_capability
from app.schemas.common import Page
from app.schemas.connector import (
    ChunkingPreviewRequest,
    ChunkingPreviewResponse,
    ConnectorCreateRequest,
    ConnectorResponse,
    ConnectorUpdateRequest,
    DocumentChunk,
    DocumentChunksResponse,
    DocumentResponse,
    ReindexRequest,
    ReindexSummary,
    ResyncResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    UploadOutcomeResponse,
    UploadResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.schemas.summarization import SummaryEditRequest
from app.services.connectors import ConnectorService
from app.services.permissions import Capability

router = APIRouter(tags=["connectors"])

_Service = Annotated[ConnectorService, Depends(get_connector_service)]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]

_reads = Depends(require_capability(Capability.ORG_READ))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))


@router.get("/connectors", dependencies=[_reads])
async def list_connectors(
    actor: CurrentActor,
    service: _Service,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[ConnectorResponse]:
    page = await service.list_connectors(actor, cursor=cursor, limit=limit)
    return Page(
        items=[ConnectorResponse.of(view) for view in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/connectors", status_code=status.HTTP_201_CREATED, dependencies=[_writes])
async def create_connector(
    body: ConnectorCreateRequest,
    actor: CurrentActor,
    service: _Service,
) -> ConnectorResponse:
    return ConnectorResponse.of(await service.create_connector(actor, body.to_draft()))


@router.get("/connectors/{connector_id}", dependencies=[_reads])
async def get_connector(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> ConnectorResponse:
    return ConnectorResponse.of(await service.get_connector(actor, connector_id))


@router.patch("/connectors/{connector_id}", dependencies=[_writes])
async def update_connector(
    connector_id: uuid.UUID,
    body: ConnectorUpdateRequest,
    actor: CurrentActor,
    service: _Service,
) -> ConnectorResponse:
    """Partial. A chunking change comes back with ``reindex_required`` set when there are
    already-indexed documents, because the stored chunks no longer match the settings."""
    return ConnectorResponse.of(
        await service.update_connector(actor, connector_id, body.to_patch())
    )


@router.delete(
    "/connectors/{connector_id}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_writes],
)
async def delete_connector(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    """202, not 204. The row is marked ``deleting`` immediately; the objects and vectors
    go in a job, and pretending otherwise would make the UI show a connector that is
    still visibly there as already gone."""
    await service.delete_connector(actor, connector_id)


@router.get("/connectors/{connector_id}/documents", dependencies=[_reads])
async def list_documents(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    status_filter: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[DocumentResponse]:
    page = await service.list_documents(
        actor, connector_id, status=status_filter, cursor=cursor, limit=limit
    )
    return Page(
        items=[
            DocumentResponse.of(document, stale=document.id in page.stale)
            for document in page.items
        ],
        next_cursor=page.next_cursor,
    )


@router.post("/connectors/{connector_id}/upload", dependencies=[_writes])
async def upload(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    files: Annotated[list[UploadFile], File()],
) -> UploadResponse:
    """Multipart, many files per call, streamed to object storage.

    Always 200: see the module docstring. A file that was rejected says so in its own
    entry, with the reason.
    """
    outcomes = await service.upload(actor, connector_id, files)
    return UploadResponse(files=[UploadOutcomeResponse.of(outcome) for outcome in outcomes])


@router.post("/connectors/{connector_id}/upload-url", dependencies=[_writes])
async def upload_url(
    connector_id: uuid.UUID,
    body: UploadUrlRequest,
    actor: CurrentActor,
    service: _Service,
) -> UploadUrlResponse:
    """A short-lived presigned ``PUT`` so customers can script uploads.

    The object it creates is picked up by the next **Resync** rather than by an event
    hook — SPEC §9.1 makes the notification a production optimization, and reconciliation
    has to exist regardless.
    """
    return UploadUrlResponse.of(await service.presigned_upload(actor, connector_id, body.filename))


@router.post("/connectors/{connector_id}/resync", dependencies=[_writes])
async def resync(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> ResyncResponse:
    """Reconcile against the source and report what changed."""
    return ResyncResponse.of(await service.resync(actor, connector_id))


@router.post("/connectors/{connector_id}/search", dependencies=[_reads])
async def search(
    connector_id: uuid.UUID,
    body: SearchRequest,
    actor: CurrentActor,
    service: _Service,
) -> SearchResponse:
    """Debug-only semantic search over one connector's chunks."""
    hits = await service.search(actor, connector_id, body.query, limit=body.limit)
    return SearchResponse(
        hits=[SearchHit.of(match) for match in hits],
        embedding_model=service.embedding_model,
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_writes],
)
async def delete_document(
    document_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    """The document, its object and its vectors. Synchronous: it is bounded work, and the
    row is on screen in front of whoever pressed it."""
    await service.delete_document(actor, document_id)


@router.get("/documents/{document_id}/chunks", dependencies=[_reads])
async def document_chunks(
    document_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    limit: int = Query(default=100, ge=1, le=500),
) -> DocumentChunksResponse:
    """The chunk inspector: what this document actually became, in cut order.

    A read, so it sits behind the read capability rather than the write one — looking at
    what was indexed is not a change, and the people who most need to look at it are the
    ones who cannot change anything.
    """
    found = await service.document_chunks(actor, document_id, limit=limit)
    return DocumentChunksResponse(
        chunks=[DocumentChunk.of(chunk) for chunk in found.chunks],
        chunk_count=found.chunk_count,
    )


@router.post("/connectors/{connector_id}/chunking/preview", dependencies=[_reads])
async def preview_chunking(
    connector_id: uuid.UUID,
    body: ChunkingPreviewRequest,
    actor: CurrentActor,
    service: _Service,
) -> ChunkingPreviewResponse:
    """**Compare**: run candidate chunking configurations over one document.

    A read, and behind the read capability, because it changes nothing — but it is the one
    read in this router that *spends money*, at the embedding provider, on every call. Hence
    the document-size ceiling in the service and the cap on candidates in the schema. The
    connector in the path is checked rather than decorative: the baseline every candidate is
    compared against is that connector's effective configuration for this document's format.
    """
    return ChunkingPreviewResponse.of(
        await service.preview_chunking(
            actor,
            connector_id,
            body.document_id,
            candidates=body.candidates,
            query=body.query,
        )
    )


@router.post("/connectors/{connector_id}/reindex", dependencies=[_writes])
async def reindex_connector(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    body: ReindexRequest | None = None,
) -> ReindexSummary:
    """Re-run ingestion for this connector's documents, which is how a chunking change is
    applied.

    Not the same operation as ``POST /platform/reindex``, despite the name they share. That
    one re-embeds chunks that are still correct under a new model; this one exists because a
    changed ``chunk_size`` makes the chunks themselves wrong, and only running the pipeline
    again fixes that. The connector detail screen offers it exactly when ``reindex_required``
    comes back set, and passes ``reindex_formats`` straight back as ``formats`` — so adding
    a per-format override re-runs the files it applies to and leaves the rest indexed.
    """
    return ReindexSummary(
        documents=await service.reindex_connector(
            actor, connector_id, formats=body.formats if body is not None else None
        )
    )


@router.post("/documents/{document_id}/reindex", dependencies=[_writes])
async def reindex_document(
    document_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> DocumentResponse:
    """The **Retry** button. Resets the row to ``pending`` and enqueues it again."""
    return DocumentResponse.of(await service.reindex_document(actor, document_id))


@router.patch("/documents/{document_id}/summary", dependencies=[_writes])
async def edit_summary(
    document_id: uuid.UUID,
    body: SummaryEditRequest,
    actor: CurrentActor,
    service: _Service,
) -> DocumentResponse:
    """Replace the document's summary with the operator's own (task 102).

    The edit re-embeds what depends on it — the summary chunk, and under ``contextual``
    every chunk — and charges no cap: no model was called. ``summary_model`` reads
    ``manual`` from here on, and the summary survives later reindexes of the same bytes.
    """
    return DocumentResponse.of(await service.edit_summary(actor, document_id, body.summary))


@router.post("/documents/{document_id}/summarize", dependencies=[_writes])
async def summarize_document(
    document_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> DocumentResponse:
    """**Regenerate**, and the **Summarize** retry after a failed summary (task 102).

    Asks the model again and re-embeds what depends on the answer. Just the summary phase
    under ``summary_chunk``; the whole ingestion under ``contextual``, where every vector
    carries the prefix.
    """
    return DocumentResponse.of(await service.regenerate_summary(actor, document_id))
