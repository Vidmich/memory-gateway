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
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor
from app.db.models import Connector, Document, ReprocessingRun
from app.db.models.connector import CONNECTOR_TYPES, TERMINAL_DOCUMENT_STATUSES
from app.schemas.config import merge_config
from app.schemas.connector_config import ChunkingConfig, changed_formats, effective
from app.schemas.summarization import (
    SummarizationConfig,
    adds_summary_chunk,
    prefixes_context,
)
from app.schemas.summarization import changed_formats as changed_summarization
from app.schemas.summarization import effective as summarization_for
from app.services.audit import Target, summarize
from app.services.audit_snapshots import subject, target_of
from app.services.chunking_preview import (
    ChunkingPreviewer,
    PreviewResult,
    PreviewTooLarge,
    candidates_from,
)
from app.services.connector_source import storage_prefix
from app.services.connector_store import (
    ConnectorStore,
    ConnectorTransaction,
    DocumentDraft,
    ReprocessScope,
    StaleCounts,
)
from app.services.distillation_models import ModelChoice
from app.services.embeddings import Embedder
from app.services.filetypes import SNIFF_BYTES, format_label, sniff
from app.services.index_fingerprint import Reason, reason_sentence, stale_reason
from app.services.ingestion import IngestionPipeline, IngestionSettings, ResyncSummary
from app.services.jobs import (
    DELETE_CONNECTOR,
    INGEST_DOCUMENT,
    SUMMARIZE_DOCUMENT,
    JobOutbox,
    JobQueue,
    delete_key,
    ingest_key,
    queue_for,
    summarize_key,
)
from app.services.object_store import ObjectStore, ObjectTooLarge
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of
from app.services.reprocessing import Reprocessor
from app.services.summarization import MANUAL
from app.services.summarizer import SUMMARIZED
from app.services.vector_store import Match, Stored, VectorStore

logger = logging.getLogger(__name__)

#: Documents re-enqueued per transaction by a connector-wide reindex. Bounded so one
#: button press on a large connector is many short transactions rather than one long one.
REINDEX_PAGE = 200

#: Ceiling on a document previewed for chunking. Far below the ingestion limit on purpose:
#: a preview embeds what it reads and stores none of it, so the size that matters is the
#: size of a document whose chunks somebody can actually look at.
PREVIEW_MAX_BYTES = 2 * 1024 * 1024

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
    summarization: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorPatch:
    name: str | None = None
    description: str | None = None
    chunking: Mapping[str, Any] | None = None
    summarization: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DocumentPage(Page[Document]):
    """A page of documents plus *why* each stale one is stale (task 104).

    The status itself is on the row now — ``index_status`` — because a stored fact is what
    every screen can agree on. The reason is still a comparison, of the row's fingerprint
    against the one ingestion would write now, so it belongs with the listing that has both
    halves in hand. Rows that are current have no entry.
    """

    reasons: Mapping[uuid.UUID, Reason] = field(default_factory=dict)

    @property
    def stale(self) -> frozenset[uuid.UUID]:
        """The rows the stored status calls stale."""
        return frozenset(row.id for row in self.items if row.index_status == "stale")


@dataclass(frozen=True, slots=True)
class ConnectorView:
    connector: Connector
    #: Documents by status, for the list screen's health summary.
    counts: Mapping[str, int]
    total_bytes: int
    #: Task 104. Documents by index status: how many are stale, being reprocessed, or
    #: unrecorded. Stored on the rows, so this is a count rather than a comparison, and
    #: it reads the same after a refresh, tomorrow, and from the list.
    stale: StaleCounts = field(default_factory=StaleCounts)
    #: True when any document is stale — derived and persistent, so the PATCH response
    #: and the GET a minute later agree.
    reindex_required: bool = False
    #: The format kinds with stale documents. A set rather than a flag because a
    #: per-format override should reprocess the code files and leave the PDFs alone.
    reindex_formats: frozenset[str] = frozenset()
    #: What ingestion would write now for each format — the thing every document is
    #: compared against. Filled on the detail read, not the list.
    effective_fingerprints: Mapping[str, str] = field(default_factory=dict)
    #: The reprocessing run in flight, if one is, for the header's progress bar.
    reprocessing: ReprocessingRun | None = None
    #: Task 102. What the connector's summarization ``model_id`` resolves to through the
    #: whole chain — its own choice, the organization's default, the distillation model,
    #: the platform's — so the panel can show the fallback greyed rather than a blank.
    #: ``None`` when nothing is configured anywhere, or on the list where it is not read.
    summary_model: ModelChoice | None = None

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
        reprocessor: Reprocessor | None = None,
    ) -> None:
        self._store = store
        self._objects = objects
        self._vectors = vectors
        self._embedder = embedder
        self._pipeline = pipeline
        self._queue = queue
        self._settings = settings or IngestionSettings()
        #: Task 104. Marks documents stale on a save and runs the reprocess. ``None``
        #: only in a build that has no runs, where a save still records the change and
        #: the nightly reconciliation catches the statuses up.
        self._reprocessor = reprocessor

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
            stale = await transaction.index_status_counts(ids)
        return Page(
            items=tuple(
                ConnectorView(
                    connector=row,
                    counts=dict(counts.get(row.id, {})),
                    total_bytes=sizes.get(row.id, 0),
                    stale=stale.get(row.id, StaleCounts()),
                    reindex_required=stale.get(row.id, StaleCounts()).stale > 0,
                )
                for row in page.items
            ),
            next_cursor=page.next_cursor,
        )

    async def get_connector(self, actor: Actor, connector_id: uuid.UUID) -> ConnectorView:
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
        return await self._detail(actor, connector)

    async def _detail(self, actor: Actor, connector: Connector) -> ConnectorView:
        """The detail view: counts, the stale summary, what every row is compared
        against, and the run in flight. The same for a GET and for the PATCH that caused
        the staleness, which is the whole point — the two agree because both read it."""
        async with self._store.begin(actor.scope) as transaction:
            counts = await transaction.document_counts([connector.id])
            sizes = await transaction.document_bytes([connector.id])
            stale = (await transaction.index_status_counts([connector.id])).get(
                connector.id, StaleCounts()
            )
            formats = frozenset(
                format_label(mime or "")
                for mime in await transaction.stale_mime_types(connector.id)
            )
        running = (
            await self._reprocessor.running(actor, connector.id)
            if self._reprocessor is not None
            else None
        )
        return ConnectorView(
            connector=connector,
            counts=dict(counts.get(connector.id, {})),
            total_bytes=sizes.get(connector.id, 0),
            summary_model=await self._summary_model(connector),
            stale=stale,
            reindex_required=stale.stale > 0,
            reindex_formats=formats,
            effective_fingerprints=await self._pipeline.expected_fingerprints(connector),
            reprocessing=running,
        )

    async def _summary_model(self, connector: Connector) -> ModelChoice | None:
        config = SummarizationConfig.load(connector.summarization)
        return await self._pipeline.summary_model(connector.organization_id, config.model_id)

    async def list_documents(
        self,
        actor: Actor,
        connector_id: uuid.UUID,
        *,
        status: str | None = None,
        index_status: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> DocumentPage:
        size = clamp_limit(limit)
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            rows = await transaction.documents(
                connector_id,
                after=decode_cursor(cursor),
                limit=size,
                status=status,
                index_status=index_status,
            )
        page = page_of(rows, limit=size, cursor_of=lambda row: row.id)
        expected = await self._pipeline.expected_fingerprints(connector)
        return DocumentPage(
            items=page.items,
            next_cursor=page.next_cursor,
            reasons={
                row.id: reason
                for row in page.items
                if (reason := self._reason(row, expected, connector)) is not None
            },
        )

    def _reason(
        self, document: Document, expected: Mapping[str, str], connector: Connector
    ) -> Reason | None:
        """Why this row is stale, in words a person can act on (task 104).

        Only for rows the stored status says are stale, and for indexed rows with no
        fingerprint at all — which are *unrecorded*, shown differently, and never counted
        as stale, because a blank is not known to be wrong. A row the status calls current
        gets no reason even if the comparison would find one: the status is the index and
        the nightly reconciliation is what corrects it, not a listing.
        """
        kind = format_label(document.mime_type or "")
        current = expected.get(kind, "")
        if document.status == "indexed" and document.index_fingerprint is None:
            code = "unrecorded"
        elif document.index_status == "stale":
            code = stale_reason(document.index_fingerprint, current) or "chunking"
        else:
            return None
        return Reason(
            code=code,
            sentence=reason_sentence(
                code,
                was={
                    "embedding_model": document.embedding_model,
                    "tokenizer": document.tokenizer,
                    "chunk_strategy": document.chunk_strategy,
                },
                now={
                    "embedding_model": self._embedder.model,
                    "tokenizer": self._pipeline.tokenizer.name,
                    # The connector is passed in rather than read off the row: the
                    # relationship would lazy-load, which an async session refuses.
                    "chunk_strategy": _effective_strategy(connector, kind),
                },
            ),
        )

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
        summarization = merge_config(
            SummarizationConfig, {}, draft.summarization, field="summarization"
        )

        async with self._store.begin(actor.scope) as transaction:
            if await transaction.name_taken(name):
                raise Conflict(f"A connector called '{name}' already exists.")
            connector = Connector(
                id=uuid7(),
                name=name,
                description=_trimmed(draft.description),
                type=draft.type,
                chunking=chunking,
                summarization=summarization,
                status="ready",
            )
            await transaction.add_connector(connector)
            # Derived from the two ids, which is why it is set after the insert rather
            # than supplied: a prefix a caller could choose is a prefix a caller could
            # point at another tenant.
            connector.storage_prefix = storage_prefix(
                organization_id=connector.organization_id, connector_id=connector.id
            )
            transaction.audit(
                actor,
                "connector.create",
                after=subject(connector),
                # Named from the row, not from the actor's scope: a platform
                # administrator has none, and the event belongs to the customer.
                organization_id=connector.organization_id,
            )
            await transaction.commit()

        logger.info(
            "connector created",
            extra={"connector_id": str(connector.id), "audit_action": "connector.create"},
        )
        return ConnectorView(
            connector=connector,
            counts={},
            total_bytes=0,
            summary_model=await self._summary_model(connector),
        )

    async def update_connector(
        self, actor: Actor, connector_id: uuid.UUID, patch: ConnectorPatch
    ) -> ConnectorView:
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            recorded = subject(connector)
            before = ChunkingConfig.load(connector.chunking)
            summarized_before = SummarizationConfig.load(connector.summarization)

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
            if patch.summarization is not None:
                connector.summarization = merge_config(
                    SummarizationConfig,
                    connector.summarization,
                    patch.summarization,
                    field="summarization",
                )

            after = ChunkingConfig.load(connector.chunking)
            # Two rules, one answer. A chunking change invalidates the chunks; a
            # `contextual` summarization change invalidates the vectors. The screen asks
            # one question — reindex what? — and gets the union.
            changed = changed_formats(before, after) | changed_summarization(
                summarized_before, SummarizationConfig.load(connector.summarization)
            )
            # A chunking change is the one edit here with consequences beyond the row —
            # every existing chunk is now stale — so the diff naming `chunking.*` is what
            # explains a reindex that follows it.
            transaction.audit(
                actor,
                "connector.update",
                before=recorded,
                after=subject(connector),
                organization_id=connector.organization_id,
            )
            await transaction.commit()

        if changed:
            # Task 104. The change is now a stored fact on every affected row, not a flag
            # in this response: one UPDATE per format, comparing each row's fingerprint
            # with what ingestion would write now — so reverting the setting un-marks
            # them just as well.
            marked = await self._mark_stale(actor, connector, sorted(changed))
            logger.info(
                "connector configuration changed; existing chunks are now stale",
                extra={
                    "connector_id": str(connector_id),
                    "formats": sorted(changed),
                    "documents": marked,
                },
            )
        return await self._detail(actor, connector)

    async def _mark_stale(self, actor: Actor, connector: Connector, kinds: Sequence[str]) -> int:
        if self._reprocessor is not None:
            return await self._reprocessor.mark_after_save(actor.scope, connector, kinds)
        expected = await self._pipeline.expected_fingerprints(connector)
        async with self._store.begin(actor.scope) as transaction:
            marked = await transaction.reconcile_index_status(connector.id, expected, kinds=kinds)
            await transaction.commit()
        return marked

    async def stale_preview(
        self, actor: Actor, connector_id: uuid.UUID, patch: ConnectorPatch
    ) -> dict[str, int]:
        """How many indexed documents a save of ``patch`` would mark stale, per format —
        what the form says *before* saving (task 104), fed by the same fingerprint diff
        the save uses, so chunking, summarization and anything later get it for free."""
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            before = ChunkingConfig.load(connector.chunking)
            summarized_before = SummarizationConfig.load(connector.summarization)
            chunking = (
                merge_config(ChunkingConfig, connector.chunking, patch.chunking, field="chunking")
                if patch.chunking is not None
                else connector.chunking
            )
            summarization = (
                merge_config(
                    SummarizationConfig,
                    connector.summarization,
                    patch.summarization,
                    field="summarization",
                )
                if patch.summarization is not None
                else connector.summarization
            )
            changed = changed_formats(
                before, ChunkingConfig.load(chunking)
            ) | changed_summarization(summarized_before, SummarizationConfig.load(summarization))
            if not changed:
                return {}
            counts = await transaction.indexed_by_format(connector.id)
        return {kind: counts[kind] for kind in sorted(changed) if counts.get(kind)}

    # -- summaries (task 102) ---------------------------------------------

    async def edit_summary(self, actor: Actor, document_id: uuid.UUID, text: str) -> Document:
        """Replace a document's summary with the operator's own, and re-embed what depends
        on it.

        The edit is the person's: ``summary_model`` becomes ``manual``, no cap is charged,
        and the reuse rule keeps it across later reindexes of the same bytes. What has to
        be re-embedded depends on the mode — the summary point alone under
        ``summary_chunk``, every chunk under ``contextual`` — and both go through the
        pipeline's own paths rather than a shortcut here.
        """
        summary = " ".join(text.split())
        if not summary:
            raise Validation("A summary cannot be empty.", param="summary")
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("Document not found.")
            connector = await self._require(transaction, document.connector_id)
            config = summarization_for(
                SummarizationConfig.load(connector.summarization),
                format_label(document.mime_type or ""),
            )
            if config.mode == "off":
                raise Validation(
                    "Summarization is off for this document's format. Turn it on for the "
                    "connector before writing a summary, or the summary would be stored "
                    "and never embedded.",
                    param="summary",
                )
            before = subject(document)
            document.summary = summary
            document.summary_status = SUMMARIZED
            document.summary_error = None
            document.summary_model = MANUAL
            document.summary_model_id = None
            document.summary_prompt_version = None
            document.summary_tokens_in = None
            document.summary_tokens_out = None
            document.summarized_at = datetime.now(UTC)
            self._queue_summary_work(outbox, document, config.mode, regenerate=False)
            transaction.audit(
                actor,
                "document.summary.update",
                before=before,
                after=subject(document),
                organization_id=document.organization_id,
            )
            await transaction.commit()
        await outbox.flush()
        return document

    async def regenerate_summary(self, actor: Actor, document_id: uuid.UUID) -> Document:
        """**Regenerate**, and the **Summarize** retry after a failure: ask the model again.

        The stored summary is cleared first so the reuse rule cannot hand it back, then the
        same job the pipeline would have run is enqueued — the whole ingestion under
        ``contextual``, the summary phase alone under ``summary_chunk``. Charged to the
        cap like any other call, because it is one.
        """
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("Document not found.")
            connector = await self._require(transaction, document.connector_id)
            config = summarization_for(
                SummarizationConfig.load(connector.summarization),
                format_label(document.mime_type or ""),
            )
            if config.mode == "off":
                raise Validation(
                    "Summarization is off for this document's format.", param="document_id"
                )
            before = subject(document)
            document.summary_status = None
            document.summary_error = None
            self._queue_summary_work(outbox, document, config.mode, regenerate=True)
            transaction.audit(
                actor,
                "document.summarize",
                before=before,
                after=subject(document),
                organization_id=document.organization_id,
            )
            await transaction.commit()
        await outbox.flush()
        return document

    @staticmethod
    def _queue_summary_work(
        outbox: JobOutbox, document: Document, mode: str, *, regenerate: bool
    ) -> None:
        payload = {
            "organization_id": str(document.organization_id),
            "document_id": str(document.id),
        }
        verb = "regenerate" if regenerate else "reembed"
        if prefixes_context(mode) or document.status != "indexed":
            # Every vector depends on the prefix, so this is the ordinary ingestion —
            # which reuses a manual summary and regenerates a cleared one. A document
            # that is not indexed has no summary point to update on its own either.
            document.status = "pending"
            document.error = None
            document.reason = None
            document.chunk_count = 0
            document.indexed_at = None
            outbox.add(
                INGEST_DOCUMENT,
                payload,
                idempotency_key=f"{ingest_key(document.id, document.content_hash)}:{verb}",
                queue=queue_for(document.source_name),
            )
        elif adds_summary_chunk(mode):
            outbox.add(
                SUMMARIZE_DOCUMENT,
                {**payload, "regenerate": regenerate},
                idempotency_key=f"{summarize_key(document.id, document.content_hash)}:{verb}",
                queue=queue_for(document.source_name),
            )

    async def delete_connector(self, actor: Actor, connector_id: uuid.UUID) -> None:
        """Mark it going, then let the worker take the bytes and the vectors."""
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            connector = await self._require(transaction, connector_id)
            before = subject(connector)
            connector.status = "deleting"
            organization_id = connector.organization_id
            # The person's act is "asked for this to go", recorded here. The row actually
            # disappearing is the worker's, and is a second event carrying
            # ``actor_type: system`` — see ``IngestionPipeline.purge``.
            transaction.audit(
                actor,
                "connector.delete",
                before=before,
                after=subject(connector),
                organization_id=organization_id,
            )
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
            removed = subject(document)
        await self._pipeline.drop_document(
            organization_id=organization_id, document_id=document_id, delete_object=True
        )
        # Recorded *after*, in its own transaction, and this is the one hook in task 15
        # that is not in the same unit of work as the change it records. It cannot be: the
        # deletion spans three systems, and an event written alongside the row would claim
        # a removal the object store might still refuse. The cost is the narrow window in
        # which a crash loses the record of a delete that did happen.
        async with self._store.begin(actor.scope) as transaction:
            transaction.audit(
                actor, "document.delete", before=removed, organization_id=organization_id
            )
            await transaction.commit()

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

    async def preview_chunking(
        self,
        actor: Actor,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        candidates: list[dict[str, Any]] | None = None,
        query: str | None = None,
    ) -> PreviewResult:
        """Run candidate chunking configurations over one document. Writes nothing.

        **Compare**, on the connector's Chunking tab. Scoped by the connector row first and
        the document second, so an id from another organization is a 404 before an object
        is fetched — the same order as every other route here, and for the same reason.

        The connector in the path is *checked* rather than decorative: a document belonging
        to a different connector is a 404 even inside the same organization, because the
        baseline the comparison is drawn against is this connector's configuration and a
        comparison against the wrong baseline is worse than no comparison.

        Capped at :data:`PREVIEW_MAX_BYTES` rather than at the ingestion limit. This
        endpoint spends money at the embedding provider on every call and nothing it
        produces is stored, so the ceiling is the size of a document somebody can actually
        read the chunks of, not the size of a document this product can index.
        """
        async with self._store.begin(actor.scope) as transaction:
            await self._require(transaction, connector_id)
            document = await transaction.document(document_id)
            if document is None or document.connector_id != connector_id:
                raise NotFound("Document not found.")
            organization_id = document.organization_id
            if document.size_bytes > PREVIEW_MAX_BYTES:
                raise PreviewTooLarge(
                    f"'{document.source_name}' is {document.size_bytes // (1024 * 1024)} MB. "
                    f"Pick a document under {PREVIEW_MAX_BYTES // (1024 * 1024)} MB to compare "
                    "chunking on — the comparison embeds it, and a large one costs a great "
                    "deal to produce a screen nobody can read.",
                    param="document_id",
                )

        read = await self._pipeline.read_document(
            organization_id=organization_id, document_id=document_id, cap=PREVIEW_MAX_BYTES
        )
        kind = format_label(read.media_type)
        previewer = ChunkingPreviewer(embedder=self._embedder, tokenizer=self._pipeline.tokenizer)
        return await previewer.run(
            read.extracted,
            candidates_from(read.chunking, candidates, kind=kind),
            document_id=document_id,
            source_name=read.source_name,
            media_type=read.media_type,
            format_kind=kind,
            query=query,
            # The connector's summarization for this format, and the summary the row
            # already holds: the comparison shows the prefix each chunk would be embedded
            # with, and its cost line includes the call that produced it. A comparison
            # that hid half the embedding cost is the thing task 20 refused to build.
            summarization=summarization_for(read.summarization, kind),
            summary=read.summary if read.summary_status == SUMMARIZED else None,
        )

    async def reindex_document(self, actor: Actor, document_id: uuid.UUID) -> Document:
        """The **Retry** button, and the way a chunking change is applied to one file."""
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("Document not found.")
            before = subject(document)
            document.status = "pending"
            document.error = None
            document.chunk_count = 0
            document.indexed_at = None
            # Task 104. Being re-ingested is the third index status; and a document a run
            # still owns keeps the run, so the run's counters settle when this finishes
            # rather than waiting for a job that will never come.
            document.index_status = "reprocessing"
            payload = {
                "organization_id": str(document.organization_id),
                "document_id": str(document_id),
            }
            if document.reprocessing_run_id is not None:
                payload["run_id"] = str(document.reprocessing_run_id)
            outbox.add(
                INGEST_DOCUMENT,
                payload,
                # Deliberately *not* the content hash: a retry is a request to run again
                # even though nothing about the file changed, and keying on the hash would
                # make the button do nothing for an hour.
                idempotency_key=f"{ingest_key(document_id, document.content_hash)}:manual",
                queue=queue_for(document.source_name),
            )
            transaction.audit(
                actor,
                "document.reindex",
                before=before,
                after=subject(document),
                organization_id=document.organization_id,
            )
            await transaction.commit()
        await outbox.flush()
        return document

    async def reindex_connector(
        self, actor: Actor, connector_id: uuid.UUID, *, formats: Sequence[str] | None = None
    ) -> int:
        """Apply a chunking change to the documents this connector holds.

        A *different* operation from task 17's platform reindex, and the difference is
        worth stating because both are called "reindex". Changing the embedding model
        re-embeds chunks that are still correct; changing ``chunk_size`` makes the chunks
        themselves wrong, so nothing short of running the pipeline again fixes it. This is
        therefore the same path as **Retry** on one document, applied to all of them,
        rather than anything to do with collections or aliases.

        ``formats`` narrows it to the format kinds actually affected, which is what makes
        a per-format override worth having: adding one for code re-runs the code files and
        leaves a thousand PDFs indexed. ``None`` means every document, which is the right
        default and the only correct answer when the connector's own settings moved.

        Documents in a non-terminal state are skipped: they already have a job coming, and
        resetting one mid-ingestion would race the worker that is writing it.

        Paged and committed per page, rather than one transaction over the whole connector.
        A folder of ten thousand files would otherwise hold a connection and its row locks
        for as long as the enqueue takes — and a page that lands is a page whose documents
        are already on their way, which is the right partial outcome for a button somebody
        pressed by hand.

        Since task 104 this is an alias: with a reprocessor wired, it starts a tracked
        run over the named formats (or everything) and returns how many documents the run
        claimed. The untracked path below stays for a build without runs.
        """
        if self._reprocessor is not None:
            scope = (
                ReprocessScope(kind="formats", formats=frozenset(formats))
                if formats
                else ReprocessScope(kind="all")
            )
            started = await self._reprocessor.start(actor, connector_id, scope=scope)
            return started.run.total if started.created else 0
        wanted = frozenset(formats) if formats is not None else None
        queued = 0
        after: uuid.UUID | None = None
        while True:
            outbox = JobOutbox(self._queue)
            async with self._store.begin(actor.scope) as transaction:
                connector = await self._require(transaction, connector_id)
                page = await transaction.documents(connector_id, after=after, limit=REINDEX_PAGE)
                for document in page:
                    after = document.id
                    if document.status not in TERMINAL_DOCUMENT_STATUSES:
                        continue
                    if wanted is not None and format_label(document.mime_type or "") not in wanted:
                        continue
                    document.status = "pending"
                    document.error = None
                    document.chunk_count = 0
                    document.indexed_at = None
                    outbox.add(
                        INGEST_DOCUMENT,
                        {
                            "organization_id": str(document.organization_id),
                            "document_id": str(document.id),
                        },
                        # Not the plain ingest key: a re-chunk is a request to run again
                        # even though the bytes have not changed, and the plain key would
                        # make the button do nothing for an hour.
                        idempotency_key=(
                            f"{ingest_key(document.id, document.content_hash)}:rechunk"
                        ),
                        queue=queue_for(document.source_name),
                    )
                    queued += 1
                if not page:
                    transaction.audit(
                        actor,
                        "connector.reindex",
                        target=target_of(connector),
                        organization_id=connector.organization_id,
                        summary=summarize(queued),
                    )
                await transaction.commit()
            await outbox.flush()
            if not page:
                break
        logger.info(
            "connector reindex enqueued",
            extra={
                "connector_id": str(connector_id),
                "documents": queued,
                "formats": sorted(wanted) if wanted is not None else "all",
            },
        )
        return queued

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

        # One event for the batch, not one per file: dropping forty files in is one thing
        # somebody did, and forty rows would bury every other event on the screen. The
        # count and a few names are what makes it recognisable; the documents themselves
        # are on the connector's own tab.
        stored = [row.filename for row in outcomes if row.status != "rejected"]
        if stored:
            async with self._store.begin(actor.scope) as transaction:
                transaction.audit(
                    actor,
                    "connector.upload",
                    target=Target("connector", connector_id, connector.name),
                    organization_id=organization_id,
                    summary=summarize(len(stored), stored, rejected=len(outcomes) - len(stored)),
                )
                await transaction.commit()
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
            summary = await self._pipeline.resync(
                organization_id=organization_id, connector_id=connector_id
            )
        except Exception as error:
            # A reconciliation that raises would otherwise leave the connector reading
            # `syncing` forever, with nothing anywhere saying why. This is what the
            # `error` status and the `error` column exist for.
            await self._mark_failed(actor, connector_id, error)
            raise

        # The reconciliation's own counts, as one event. What changed is potentially
        # thousands of document rows, and SPEC §9.1's five numbers are a better account of
        # that than five thousand entries would be.
        async with self._store.begin(actor.scope) as transaction:
            synced = await transaction.connector(connector_id)
            if synced is not None:
                transaction.audit(
                    actor,
                    "connector.resync",
                    target=target_of(synced),
                    organization_id=organization_id,
                    summary=summarize(
                        summary.added + summary.updated + summary.deleted,
                        (),
                        added=summary.added,
                        updated=summary.updated,
                        deleted=summary.deleted,
                        unchanged=summary.unchanged,
                        skipped=summary.skipped,
                    ),
                )
                await transaction.commit()
        return summary

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


def _effective_strategy(connector: Connector, kind: str) -> str:
    return effective(ChunkingConfig.load(connector.chunking), kind).strategy


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
