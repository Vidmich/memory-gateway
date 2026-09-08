"""Connectors: the control-plane rules (SPEC §9.1, §13.1).

The service owns what the API and the worker must both agree on — who may touch a
connector, what a valid name and object key look like, when a chunking change invalidates
the index, and which of these operations are safe to do on the request path.

That last one is the judgement running through this module. Three operations could be
either synchronous or a job, and each is decided on its own merits rather than by a rule:

**Upload is synchronous** up to the point the bytes are stored, then enqueues. The customer
has to know whether their file was accepted, and a 202 for a file that is about to be
rejected as 60 MB is not an answer.

**Resync is synchronous.** SPEC §9.1 says it reports ``{added, updated, deleted,
unchanged, skipped}``, and a summary cannot be returned by a job. What it actually does is
a listing and some row writes; the expensive half — extracting and embedding each changed
object — is what it enqueues. The trade is real and worth naming: a connector with an
enormous number of objects makes this a long request, and task 18's scheduled sync is
where that becomes a background job with a progress record instead of a return value.

**Deleting a connector is a job.** It is unbounded — every object, every vector — and
nobody is waiting to read its result. The row is marked ``deleting`` first, so the UI
stops offering uploads into something that is going away.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor
from app.db.models import Connector, Document
from app.db.models.connector import CONNECTOR_TYPES
from app.schemas.config import merge_config
from app.schemas.connector_config import ChunkingConfig, requires_reindex
from app.services.connector_source import storage_prefix
from app.services.connector_store import ConnectorStore, ConnectorTransaction, DocumentDraft
from app.services.embeddings import Embedder
from app.services.filetypes import SNIFF_BYTES, sniff
from app.services.ingestion import IngestionPipeline, IngestionSettings, ResyncSummary
from app.services.jobs import (
    DELETE_CONNECTOR,
    INGEST_DOCUMENT,
    JobOutbox,
    JobQueue,
    delete_key,
    ingest_key,
    queue_for,
)
from app.services.object_store import ObjectStore, ObjectTooLarge
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of
from app.services.vector_store import Match, Stored, VectorStore

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 200

#: How long a presigned upload URL lives. Fifteen minutes is SPEC §9.1's default: long
#: enough to upload a large file over a poor connection, short enough that a URL found in
#: a shell history a week later is useless.
DEFAULT_UPLOAD_URL_TTL_SECONDS = 15 * 60

#: What a customer may call a file. Path separators are allowed so a dragged *folder*
#: keeps its shape, and everything that could escape the connector's prefix is not.
_UNSAFE_SEGMENT = re.compile(r"^\.+$")
_ILLEGAL = re.compile(r"[\x00-\x1f\\]")


class UploadedFile(Protocol):
    """One file arriving over multipart. Narrow on purpose: this is all the service uses,
    and it is what makes an upload testable without a Starlette request."""

    @property
    def filename(self) -> str | None: ...

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ConnectorDraft:
    name: str
    description: str | None = None
    type: str = "managed_file_drop"
    chunking: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorPatch:
    name: str | None = None
    description: str | None = None
    chunking: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorView:
    connector: Connector
    #: Documents by status, for the list screen's health summary.
    counts: Mapping[str, int]
    total_bytes: int
    #: True when the chunking settings changed and the stored chunks no longer match
    #: them. Set by an update, so the UI can say "reindex to apply" at the moment the
    #: change is made rather than in a banner that is always on.
    reindex_required: bool = False

    @property
    def document_count(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    filename: str
    document_id: uuid.UUID | None
    status: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Accepted:
    """One stored file, on the way to becoming an :class:`UploadOutcome`.

    Separate from the outcome because the ETag is needed for the enqueue and has no
    business being in an API response — it is a storage detail, and the one place it is
    read is the idempotency key three lines later.
    """

    name: str
    document_id: uuid.UUID
    size_bytes: int
    etag: str | None


@dataclass(frozen=True, slots=True)
class DocumentChunks:
    """What the inspector shows: the chunks, and what the row claims it has.

    Both, because the two disagreeing is itself the finding — a document reporting twelve
    chunks with three in the index was written into a collection that has since been
    dropped, and the inspector is where that becomes visible instead of "retrieval is bad".
    """

    chunks: list[Stored]
    chunk_count: int


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    key: str
    expires_in: int


class ConnectorService:
    def __init__(
        self,
        store: ConnectorStore,
        *,
        objects: ObjectStore,
        vectors: VectorStore,
        embedder: Embedder,
        pipeline: IngestionPipeline,
        queue: JobQueue,
        settings: IngestionSettings | None = None,
    ) -> None:
        self._store = store
        self._objects = objects
        self._vectors = vectors
        self._embedder = embedder
        self._pipeline = pipeline
        self._queue = queue
        self._settings = settings or IngestionSettings()

    @property
    def embedding_model(self) -> str:
        """Which model produced the vectors in this index. Returned with every debug
        search, because two searches under different models are not comparable and there
        is nowhere else the difference would show."""
        return self._embedder.model

    # -- reads -----------------------------------------------------------

    async def list_connectors(
        self, actor: Actor, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[ConnectorView]:
        size = clamp_limit(limit)
        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.connectors(after=decode_cursor(cursor), limit=size)
            page = page_of(rows, limit=size, cursor_of=lambda row: row.id)
            ids = [row.id for row in page.items]
            counts = await transaction.document_counts(ids)
            sizes = await transaction.document_bytes(ids)
        return Page(
            items=tuple(
                ConnectorView(
                    connector=row,
                    counts=dict(counts.get(row.id, {})),
                    total_bytes=sizes.get(row.id, 0),
                )
                for row in page.items
            ),
            next_cursor=page.next_cursor,
        )

    async def get_connector(self, actor: Actor, connector_id: uuid.UUID) -> ConnectorView:
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            counts = await transaction.document_counts([connector_id])
            sizes = await transaction.document_bytes([connector_id])
        return ConnectorView(
            connector=connector,
            counts=dict(counts.get(connector_id, {})),
            total_bytes=sizes.get(connector_id, 0),
        )

    async def list_documents(
        self,
        actor: Actor,
        connector_id: uuid.UUID,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[Document]:
        size = clamp_limit(limit)
        async with self._store.begin(actor.scope) as transaction:
            await self._require(transaction, connector_id)
            rows = await transaction.documents(
                connector_id, after=decode_cursor(cursor), limit=size, status=status
            )
        return page_of(rows, limit=size, cursor_of=lambda row: row.id)

    # -- writes ----------------------------------------------------------

    async def create_connector(self, actor: Actor, draft: ConnectorDraft) -> ConnectorView:
        name = _name(draft.name)
        if draft.type not in CONNECTOR_TYPES:
            raise Validation(
                f"'{draft.type}' is not a connector type this build supports. "
                f"Available: {', '.join(CONNECTOR_TYPES)}.",
                param="type",
            )
        chunking = merge_config(ChunkingConfig, {}, draft.chunking, field="chunking")

        async with self._store.begin(actor.scope) as transaction:
            if await transaction.name_taken(name):
                raise Conflict(f"A connector called '{name}' already exists.")
            connector = Connector(
                id=uuid7(),
                name=name,
                description=_trimmed(draft.description),
                type=draft.type,
                chunking=chunking,
                status="ready",
            )
            await transaction.add_connector(connector)
            # Derived from the two ids, which is why it is set after the insert rather
            # than supplied: a prefix a caller could choose is a prefix a caller could
            # point at another tenant.
            connector.storage_prefix = storage_prefix(
                organization_id=connector.organization_id, connector_id=connector.id
            )
            await transaction.commit()

        logger.info(
            "connector created",
            extra={"connector_id": str(connector.id), "audit_action": "connector.create"},
        )
        return ConnectorView(connector=connector, counts={}, total_bytes=0)

    async def update_connector(
        self, actor: Actor, connector_id: uuid.UUID, patch: ConnectorPatch
    ) -> ConnectorView:
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            before = ChunkingConfig.load(connector.chunking)

            if patch.name is not None:
                name = _name(patch.name)
                if await transaction.name_taken(name, excluding=connector_id):
                    raise Conflict(f"A connector called '{name}' already exists.")
                connector.name = name
            if patch.description is not None:
                connector.description = _trimmed(patch.description)
            if patch.chunking is not None:
                connector.chunking = merge_config(
                    ChunkingConfig, connector.chunking, patch.chunking, field="chunking"
                )

            after = ChunkingConfig.load(connector.chunking)
            changed = requires_reindex(before, after)
            await transaction.commit()

            counts = await transaction.document_counts([connector_id])
            sizes = await transaction.document_bytes([connector_id])

        if changed:
            logger.info(
                "connector chunking changed; existing chunks are now stale",
                extra={"connector_id": str(connector_id)},
            )
        return ConnectorView(
            connector=connector,
            counts=dict(counts.get(connector_id, {})),
            total_bytes=sizes.get(connector_id, 0),
            # Only meaningful when something is actually indexed. Telling somebody to
            # reindex an empty connector is noise they will learn to ignore.
            reindex_required=changed and bool(counts.get(connector_id)),
        )

    async def delete_connector(self, actor: Actor, connector_id: uuid.UUID) -> None:
        """Mark it going, then let the worker take the bytes and the vectors."""
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            connector.status = "deleting"
            organization_id = connector.organization_id
            outbox.add(
                DELETE_CONNECTOR,
                {"organization_id": str(organization_id), "connector_id": str(connector_id)},
                idempotency_key=delete_key(connector_id),
            )
            await transaction.commit()
        await outbox.flush()
        logger.info(
            "connector deletion started",
            extra={"connector_id": str(connector_id), "audit_action": "connector.delete"},
        )

    async def delete_document(self, actor: Actor, document_id: uuid.UUID) -> None:
        """One document, its object and its vectors — synchronously.

        Bounded work, and the customer is looking at the row they just deleted. A job here
        would leave it on screen until a poll noticed it had gone.
        """
        async with self._store.begin(actor.scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("Document not found.")
            organization_id = document.organization_id
        await self._pipeline.drop_document(
            organization_id=organization_id, document_id=document_id, delete_object=True
        )

    async def document_chunks(
        self, actor: Actor, document_id: uuid.UUID, *, limit: int = 200
    ) -> DocumentChunks:
        """What one document actually became, in the order it was cut.

        The chunk inspector. It answers a question nothing else on the connector screen
        can: a PDF whose every chunk opens with the same page header, a spreadsheet
        indexed as bare cells, a Word file that came out as its pre-review draft all
        report ``indexed`` with a plausible chunk count and answer badly, and the only way
        to see which is to read the text.

        Scoped by the document row first, so an id from another organization is a 404
        before the vector store is touched at all.
        """
        async with self._store.begin(actor.scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("Document not found.")
            organization_id = document.organization_id
            expected = document.chunk_count
        stored = await self._vectors.chunks(
            organization_id, document_id, limit=max(1, min(limit, 500))
        )
        return DocumentChunks(chunks=stored, chunk_count=expected)

    async def reindex_document(self, actor: Actor, document_id: uuid.UUID) -> Document:
        """The **Retry** button, and the way a chunking change is applied to one file."""
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("Document not found.")
            document.status = "pending"
            document.error = None
            document.chunk_count = 0
            document.indexed_at = None
            outbox.add(
                INGEST_DOCUMENT,
                {
                    "organization_id": str(document.organization_id),
                    "document_id": str(document_id),
                },
                # Deliberately *not* the content hash: a retry is a request to run again
                # even though nothing about the file changed, and keying on the hash would
                # make the button do nothing for an hour.
                idempotency_key=f"{ingest_key(document_id, document.content_hash)}:manual",
                queue=queue_for(document.source_name),
            )
            await transaction.commit()
        await outbox.flush()
        return document

    # -- upload ----------------------------------------------------------

    async def upload(
        self, actor: Actor, connector_id: uuid.UUID, files: Sequence[UploadedFile]
    ) -> list[UploadOutcome]:
        """Stream each file to storage, then enqueue its ingestion.

        Per-file outcomes rather than one status for the batch: dropping forty files in
        and being told "400" because one of them was a 2 GB video is not an answer anybody
        can act on.

        **One short transaction per file, never one across the batch.** The bytes go to
        object storage between them, and holding a database transaction open across forty
        network writes ties up a connection — and its row locks — for as long as the
        slowest upload takes. Re-reading the connector each time costs one indexed lookup
        and buys a second thing worth having: a delete that starts mid-batch stops the
        rest of it, rather than writing rows into a connector that is going away.
        """
        outbox = JobOutbox(self._queue)
        outcomes: list[UploadOutcome] = []

        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            if connector.status == "deleting":
                raise Conflict("This connector is being deleted.")
            organization_id = connector.organization_id
            prefix = connector.storage_prefix or storage_prefix(
                organization_id=organization_id, connector_id=connector.id
            )
            used = await transaction.organization_bytes()

        for upload in files:
            try:
                accepted = await self._store_one(
                    actor, connector_id, upload, prefix=prefix, used=used
                )
            except (ObjectTooLarge, Validation) as error:
                outcomes.append(
                    UploadOutcome(
                        filename=upload.filename or "(unnamed)",
                        document_id=None,
                        status="rejected",
                        error=error.message,
                    )
                )
                continue

            used += accepted.size_bytes
            outcomes.append(
                UploadOutcome(
                    filename=accepted.name,
                    document_id=accepted.document_id,
                    status="pending",
                )
            )
            outbox.add(
                INGEST_DOCUMENT,
                {
                    "organization_id": str(organization_id),
                    "document_id": str(accepted.document_id),
                },
                idempotency_key=ingest_key(accepted.document_id, accepted.etag),
                queue=queue_for(accepted.name),
            )

        # After every commit, always. A job that names a document row a rollback removed
        # is a worker failure nobody can explain from the evidence.
        await outbox.flush()
        return outcomes

    async def _store_one(
        self,
        actor: Actor,
        connector_id: uuid.UUID,
        upload: UploadedFile,
        *,
        prefix: str,
        used: int,
    ) -> _Accepted:
        name = safe_key(upload.filename or "")
        quota = self._settings.storage_quota_bytes
        if quota is not None and used >= quota:
            raise Validation(
                f"This organization has used its {quota // (1024 * 1024)} MB of storage. "
                "Delete some documents or ask for more."
            )

        head = await upload.read(SNIFF_BYTES)
        media_type = sniff(head, name=name)
        key = f"{prefix}{name}"
        allowance = self._settings.max_file_bytes
        if quota is not None:
            allowance = min(allowance, quota - used)

        # The bytes first, outside any transaction. Everything below is one short write.
        try:
            stored = await self._objects.put(
                key,
                _rest_of(upload, head),
                content_type=media_type,
                max_bytes=allowance,
            )
        except ObjectTooLarge:
            if quota is not None and allowance < self._settings.max_file_bytes:
                # The file was not too big; the organization is out of room. Reporting the
                # per-file limit here would send somebody to split a file that would not
                # have fitted either way.
                raise Validation(
                    f"This upload would exceed the organization's "
                    f"{quota // (1024 * 1024)} MB of storage. Delete some documents or "
                    "ask for more."
                ) from None
            raise

        async with self._store.begin(actor.scope) as transaction:
            connector = await transaction.connector(connector_id)
            if connector is None:
                raise Validation("This connector no longer exists.")
            if connector.status == "deleting":
                raise Validation("This connector is being deleted.")
            document = await transaction.claim_document(
                connector,
                DocumentDraft(
                    source_uri=key,
                    source_name=name,
                    size_bytes=stored.size_bytes,
                    etag=stored.etag,
                    mime_type=media_type,
                ),
                reset=True,
            )
            await transaction.commit()

        return _Accepted(
            name=name,
            document_id=document.id,
            size_bytes=stored.size_bytes,
            etag=stored.etag,
        )

    def upload_url(
        self, connector: Connector, filename: str, *, expires_in: int | None = None
    ) -> PresignedUpload:
        """A URL a script can ``PUT`` to.

        The object it produces is picked up by the next **Resync**, not by an event hook —
        SPEC §9.1 makes the notification a production optimization, and a resync is the
        reconciliation that has to exist regardless.
        """
        prefix = connector.storage_prefix or storage_prefix(
            organization_id=connector.organization_id, connector_id=connector.id
        )
        key = f"{prefix}{safe_key(filename)}"
        ttl = expires_in or DEFAULT_UPLOAD_URL_TTL_SECONDS
        return PresignedUpload(
            url=self._objects.presign_put(key, expires_in=ttl), key=key, expires_in=ttl
        )

    async def presigned_upload(
        self, actor: Actor, connector_id: uuid.UUID, filename: str
    ) -> PresignedUpload:
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
        return self.upload_url(connector, filename)

    # -- resync and search -----------------------------------------------

    async def resync(self, actor: Actor, connector_id: uuid.UUID) -> ResyncSummary:
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            if connector.status == "deleting":
                raise Conflict("This connector is being deleted.")
            organization_id = connector.organization_id
            connector.status = "syncing"
            await transaction.commit()

        try:
            return await self._pipeline.resync(
                organization_id=organization_id, connector_id=connector_id
            )
        except Exception as error:
            # A reconciliation that raises would otherwise leave the connector reading
            # `syncing` forever, with nothing anywhere saying why. This is what the
            # `error` status and the `error` column exist for.
            await self._mark_failed(actor, connector_id, error)
            raise

    async def _mark_failed(self, actor: Actor, connector_id: uuid.UUID, error: Exception) -> None:
        try:
            async with self._store.begin(actor.scope) as transaction:
                connector = await transaction.connector(connector_id)
                if connector is None or connector.status == "deleting":
                    return
                connector.status = "error"
                connector.error = _one_line(error)
                await transaction.commit()
        except Exception:
            # The original failure is the one worth raising; losing the annotation is a
            # worse screen, not a worse outcome.
            logger.warning(
                "could not record the connector failure",
                extra={"connector_id": str(connector_id)},
                exc_info=True,
            )

    async def search(
        self, actor: Actor, connector_id: uuid.UUID, query: str, *, limit: int = 10
    ) -> list[Match]:
        """The debug search. Proves the index works before task 10 exists.

        Scoped to one connector *and* to the caller's organization, and both matter: the
        collection is per tenant, so the second is structural, and the first is what makes
        the result answer "did this connector index my file" rather than "is there
        anything like this anywhere".
        """
        text = query.strip()
        if not text:
            raise Validation("Enter something to search for.", param="query")
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            organization_id = connector.organization_id
        vectors = await self._embedder.embed([text])
        return await self._vectors.search(
            organization_id,
            vectors[0],
            connector_ids=[connector_id],
            limit=max(1, min(limit, 50)),
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    async def _require(transaction: ConnectorTransaction, connector_id: uuid.UUID) -> Connector:
        connector = await transaction.connector(connector_id)
        if connector is None:
            # The same 404 whether it does not exist or belongs to another organization.
            raise NotFound("Connector not found.")
        return connector


async def _rest_of(upload: UploadedFile, head: bytes) -> AsyncIterator[bytes]:
    """The already-read head, then the remainder, in chunks."""
    if head:
        yield head
    while True:
        piece = await upload.read(1024 * 1024)
        if not piece:
            return
        yield piece


def safe_key(filename: str) -> str:
    """A filename turned into a key suffix that cannot leave the connector's prefix.

    Path separators survive, because dragging in a folder should keep its structure and
    ``docs/api/auth.md`` is more useful in a citation than ``auth.md``. Everything that
    could climb out of the prefix does not: no leading slash, no drive letter, no ``..``
    segment, no control characters, no backslash.
    """
    # Backslashes become separators *before* the illegal-character pass, not after:
    # a Windows client sends `docspiuth.md`, and stripping the separator first
    # would silently glue three path segments into one filename.
    cleaned = _ILLEGAL.sub("", filename.replace("\\", "/")).strip()
    segments = [part.strip() for part in cleaned.split("/")]
    kept = [part for part in segments if part and part != "." and not _UNSAFE_SEGMENT.match(part)]
    key = "/".join(kept)
    if not key:
        raise Validation("A file needs a name.", param="file")
    if len(key) > 1024:
        raise Validation("This file's name is too long to store.", param="file")
    return key


def _name(value: str) -> str:
    name = value.strip()
    if not name:
        raise Validation("A connector needs a name.", param="name")
    if len(name) > MAX_NAME_LENGTH:
        raise Validation(
            f"A connector name can be at most {MAX_NAME_LENGTH} characters.", param="name"
        )
    return name


def _one_line(error: Exception) -> str:
    """A sentence for the connector row. Never a traceback: this is rendered to a
    customer, next to a Resync button they are about to press again."""
    message = getattr(error, "message", None) or str(error) or error.__class__.__name__
    return str(message).strip().splitlines()[0][:500]


def _trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


__all__ = [
    "DEFAULT_UPLOAD_URL_TTL_SECONDS",
    "ConnectorDraft",
    "ConnectorPatch",
    "ConnectorService",
    "ConnectorView",
    "PresignedUpload",
    "UploadOutcome",
    "UploadedFile",
    "safe_key",
]
