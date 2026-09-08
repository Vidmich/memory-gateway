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
    ConnectorCreateRequest,
    ConnectorResponse,
    ConnectorUpdateRequest,
    DocumentChunk,
    DocumentChunksResponse,
    DocumentResponse,
    ResyncResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    UploadOutcomeResponse,
    UploadResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
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
        items=[DocumentResponse.of(document) for document in page.items],
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


@router.post("/documents/{document_id}/reindex", dependencies=[_writes])
async def reindex_document(
    document_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> DocumentResponse:
    """The **Retry** button. Resets the row to ``pending`` and enqueues it again."""
    return DocumentResponse.of(await service.reindex_document(actor, document_id))
