"""The pipeline: object bytes in, indexed chunks out.

SPEC §9.5's progression — ``pending → extracting → chunking → embedding → indexed`` — is
written to the document row at each step rather than inferred at the end. That costs four
small updates per document and buys the thing the demo is actually about: a table that
moves while you watch it, and a stuck document that says *which* step it is stuck on.

**The distinction that shapes this module is failure-vs-failure.** Two kinds, and they are
handled in opposite ways:

*The document is bad.* It will not decode, it is a format nothing here reads, it is
empty. Retrying is pointless — the bytes will not change — so the row is marked ``failed``
or ``skipped`` with a sentence a customer can act on, and the job **returns normally**.
The retry that matters for this case is the button in the UI, pressed after the file has
been fixed.

*The world is bad.* The embedding provider is rate-limiting, Qdrant is restarting, object
storage timed out. The document is fine and will index perfectly in thirty seconds, so the
job **raises** and the runner's backoff does its work. Marking the document ``failed``
here would be a lie that a customer would have to notice and undo by hand.

Getting that backwards in either direction is the most expensive mistake available: one
way a provider blip permanently fails a thousand documents, the other way a corrupt file
is retried until it dead-letters and the customer is told nothing useful.

**Binary files are never read past the sniff window.** The first few kilobytes decide the
type, and a file nothing can extract stops there — a folder of videos costs a listing and
8 KB each, not their size in bytes. Supported files are then read in full because
extraction needs the content; that read is bounded by the per-file cap, which is what
makes worker memory a function of concurrency rather than of what somebody uploaded.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import NotFound
from app.core.metrics import ChunkingMetrics, ExtractionMetrics
from app.core.tenancy import TenantScope
from app.core.tracing import phase, record_error
from app.db.models import Connector, Document
from app.db.models.connector import TERMINAL_DOCUMENT_STATUSES
from app.schemas.connector_config import ChunkingConfig, effective, fingerprint
from app.schemas.summarization import (
    SummarizationConfig,
    adds_summary_chunk,
    context_identity,
    prefixes_context,
)
from app.schemas.summarization import effective as summarization_for
from app.services.audit import Attribution
from app.services.audit_snapshots import subject
from app.services.chunking import (
    BoundarySignal,
    Chunk,
    boundary_signal,
    chunk_document,
    needs_signal,
    plan_signal,
)
from app.services.connector_source import ConnectorSource, build_source
from app.services.connector_store import ConnectorStore, DocumentDraft
from app.services.distillation_models import ModelChoice
from app.services.embeddings import Embedder, EmbeddingError
from app.services.extraction import (
    Extracted,
    ExtractionError,
    ExtractorRegistry,
    Registration,
    SkippedDocument,
)
from app.services.extraction_pool import ExtractionPool
from app.services.filetypes import SNIFF_BYTES, describe, format_label, sniff
from app.services.jobs import (
    INGEST_DOCUMENT,
    SUMMARIZE_DOCUMENT,
    JobOutbox,
    JobQueue,
    JobRequest,
    PermanentJobError,
    ingest_key,
    queue_for,
    resync_key,
    summarize_key,
)
from app.services.locks import Lock
from app.services.object_store import ObjectRef, ObjectStore
from app.services.summarization import (
    KIND_SOURCE,
    KIND_SUMMARY,
    MANUAL,
    PROMPT_VERSION,
    SUMMARY_INDEX,
    SUMMARY_SECTION,
    token_estimate,
)
from app.services.summarization_store import WAITING_ON_CAP
from app.services.summarizer import CAPPED, SUMMARIZED, Summarizer, SummaryOutcome
from app.services.tokenizer import Tokenizer
from app.services.vector_store import ChunkPoint, VectorStore, point_id

logger = logging.getLogger(__name__)

#: Where the pipeline gets its tokenizer from — see ``IngestionPipeline.__init__``.
type TokenizerSource = Callable[[], Tokenizer]


def _constant(tokenizer: Tokenizer) -> TokenizerSource:
    return lambda: tokenizer


#: ``documents.reason`` when a document failed *because* its summary did, under
#: ``contextual`` (task 102). The sentence beside it names the model.
SUMMARIZATION_FAILED = "summarization"

#: Seconds past UTC midnight a capped document is retried. A small margin, so a clock
#: skewed by a second does not wake the job on the wrong side of the cap's reset.
CAP_RETRY_MARGIN_SECONDS = 30

DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    extraction_timeout_seconds: float = DEFAULT_EXTRACTION_TIMEOUT_SECONDS
    #: Per-organization ceiling on stored bytes. ``None`` is unlimited, which is the
    #: default for the same reason task 06's rate limits default to unlimited: a quota
    #: nobody set should not become an outage.
    storage_quota_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResyncSummary:
    """What one reconciliation did. SPEC §9.1's four cases, plus what it declined to touch.

    ``skipped`` counts documents left alone because ingestion was already in flight for
    them. That is not the same as ``unchanged`` — one means "nothing to do", the other
    means "something is already doing it" — and a resync that reported them together
    would make a stuck connector look healthy.
    """

    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.added + self.updated + self.deleted + self.unchanged + self.skipped


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    status: str
    chunk_count: int = 0
    error: str | None = None
    #: The machine-readable half of ``error``. See ``documents.reason``.
    reason: str | None = None
    #: Pages, slides or sheets, where the format has such a unit.
    page_count: int | None = None
    #: How this document was cut, for ``documents.chunk_strategy`` and
    #: ``documents.chunk_fingerprint``. The *effective* configuration's, so a connector
    #: with a per-format override reports what actually happened to this file rather than
    #: what the connector's top-level setting says.
    chunk_strategy: str | None = None
    chunk_fingerprint: str | None = None
    #: What ``chunk_size`` was measured with (task 101), by the tokenizer's own name — so
    #: a vocabulary that failed to load is recorded as the word fallback it actually was.
    tokenizer: str | None = None


@dataclass(frozen=True, slots=True)
class ReadDocument:
    """One document, fetched and extracted, on its way to a preview rather than an index."""

    extracted: Extracted
    media_type: str
    source_name: str
    size_bytes: int
    #: The connector's stored configuration, unresolved. The caller applies the per-format
    #: override, because the caller is the one that knows which candidates it is comparing
    #: it against.
    chunking: ChunkingConfig
    connector_id: uuid.UUID
    source_uri: str
    content_hash: str
    #: Task 102. The connector's summarization settings, unresolved like ``chunking``, and
    #: the summary the row already holds — so a recut and a **Compare** can carry the
    #: stored prefix without calling a model.
    summarization: SummarizationConfig = field(default_factory=SummarizationConfig)
    summary: str | None = None
    summary_status: str | None = None


@dataclass(frozen=True, slots=True)
class _Summarized:
    """What the summarization phase decided for one document.

    ``summary`` is the text to use — from the model, or reused from the row — or ``None``
    when the document goes on without one. ``outcome`` is set when the document does
    *not* go on: parked on the cap, or failed, both under ``contextual``.
    """

    summary: str | None
    model_id: uuid.UUID | None
    outcome: IngestOutcome | None = None


@dataclass(frozen=True, slots=True)
class _Read:
    """What one streamed read produced."""

    data: bytes
    media_type: str
    content_hash: str
    size_bytes: int
    #: Set when the file was decided against before being read in full.
    outcome: IngestOutcome | None = None


class IngestionPipeline:
    """Everything a worker does, with no worker in it.

    Takes ports and returns outcomes, so the whole of ingestion — including the two
    reconciliation paths — is exercised in tests without Redis, S3, Qdrant or a provider.
    """

    def __init__(
        self,
        store: ConnectorStore,
        *,
        objects: ObjectStore,
        vectors: VectorStore,
        embedder: Embedder,
        tokenizer: Tokenizer | TokenizerSource,
        registry: ExtractorRegistry,
        queue: JobQueue,
        lock: Lock,
        settings: IngestionSettings | None = None,
        pool: ExtractionPool | None = None,
        metrics: ExtractionMetrics | None = None,
        chunking_metrics: ChunkingMetrics | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self._store = store
        self._objects = objects
        self._vectors = vectors
        self._embedder = embedder
        #: Task 102. ``None`` means no model call can be made from this process; a
        #: connector with summarization switched on then behaves as if no model were
        #: configured, which the document row says in as many words.
        self._summarizer = summarizer
        #: A tokenizer, or a function that returns the current one. Production passes the
        #: function (task 101): the tokenizer is the embedding model's, the embedding model
        #: is a platform setting cached per worker, and a change to it mid-run is exactly
        #: the case that has to be recorded per document rather than per process. A test
        #: passes the object.
        self._tokenizers: TokenizerSource = (
            tokenizer if callable(tokenizer) else _constant(tokenizer)
        )
        self._registry = registry
        self._queue = queue
        self._lock = lock
        self._settings = settings or IngestionSettings()
        #: ``None`` runs every extractor in a thread, which is what a test wants: a
        #: subprocess pool costs a second of interpreter startup per child and buys
        #: isolation that only the isolation tests are about.
        self._pool = pool
        self._metrics = metrics
        self._chunking_metrics = chunking_metrics

    # -- ingestion -------------------------------------------------------

    async def ingest(self, *, organization_id: uuid.UUID, document_id: uuid.UUID) -> IngestOutcome:
        """Take one document from wherever it is to a terminal state."""
        scope = TenantScope.of_organization(organization_id)

        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                # The row was deleted while the job waited. Nothing to do, and nothing
                # retrying will fix.
                raise PermanentJobError(f"document {document_id} no longer exists")
            connector = await transaction.connector(document.connector_id)
            if connector is None:
                raise PermanentJobError(f"connector {document.connector_id} no longer exists")
            if connector.status == "deleting":
                # A delete job is tearing this connector down. Indexing into it now would
                # leave vectors the delete has already walked past.
                return IngestOutcome(status=document.status)

            source = self._source(connector)
            chunking = ChunkingConfig.load(connector.chunking)
            summarization = SummarizationConfig.load(connector.summarization)
            connector_id = connector.id
            reference = ObjectRef(
                key=document.source_uri, size_bytes=document.size_bytes, etag=document.etag
            )
            await self._advance(transaction, document, "extracting")

        try:
            read = await self._read(source, reference)
        except KeyError:
            # The object went away between the listing and now. Not a failure of the
            # document: the document should not exist either.
            await self._finish(
                scope,
                document_id,
                IngestOutcome(status="failed", reason="missing_object"),
                error="This file is no longer in storage. Run a resync.",
            )
            return IngestOutcome(status="failed", error="missing object")

        if read.outcome is not None:
            await self._finish(
                scope,
                document_id,
                read.outcome,
                error=read.outcome.error,
                media_type=read.media_type,
                size_bytes=read.size_bytes,
                content_hash=read.content_hash,
            )
            return read.outcome

        try:
            extracted = await self._extract(
                read.data, name=reference.name, media_type=read.media_type
            )
        except ExtractionError as error:
            # `SkippedDocument` is a decision, not a fault: a scan with no text layer will
            # reach the same decision on every retry, and calling it `failed` would send
            # somebody looking for a problem with the file that is not there.
            status = "skipped" if isinstance(error, SkippedDocument) else "failed"
            outcome = IngestOutcome(status=status, error=str(error), reason=error.reason)
            await self._finish(
                scope,
                document_id,
                outcome,
                error=str(error),
                media_type=read.media_type,
                size_bytes=read.size_bytes,
                content_hash=read.content_hash,
            )
            return outcome

        # Resolved here rather than above, because the format is only known once the bytes
        # have been sniffed — which is the whole point of per-format overrides: a
        # connector is a source, and what is in it is discovered one file at a time.
        kind = format_label(read.media_type)
        cutting = effective(chunking, kind)
        summarizing = summarization_for(summarization, kind)
        # Read once per document, so the fingerprint, the cut and the row agree even if
        # the platform setting moves while this file is in flight.
        tokenizer = self.tokenizer

        # Task 102's phase, between extraction and chunking. What a failure here means
        # depends on the mode, and `_summarize_phase` says; a retryable provider error
        # raises out of it like every other "the world is bad" case in this method.
        summarized = await self._summarize_phase(
            scope,
            organization_id=organization_id,
            connector_id=connector_id,
            document_id=document_id,
            source_name=reference.name,
            text=extracted.text,
            content_hash=read.content_hash,
            tokenizer=tokenizer,
            config=summarizing,
            queue_name=queue_for(reference.name),
        )
        if summarized.outcome is not None:
            await self._finish(
                scope,
                document_id,
                summarized.outcome,
                error=summarized.outcome.error,
                media_type=read.media_type,
                size_bytes=read.size_bytes,
                content_hash=read.content_hash,
            )
            return summarized.outcome

        async with self._store.begin(scope) as transaction:
            document = await self._require(transaction, document_id)
            await self._advance(transaction, document, "chunking")

        cut = fingerprint(
            cutting,
            embedding_model=self._embedder.model,
            tokenizer=tokenizer.name,
            context=context_identity(
                summarizing, summarized.model_id, prompt_version=PROMPT_VERSION
            ),
        )
        try:
            chunks = await self._chunk(
                extracted, cutting, media_type=read.media_type, tokenizer=tokenizer
            )
        except EmbeddingError as error:
            if error.retryable:
                # The world is bad, not the document. This file will chunk perfectly in
                # thirty seconds, so the job raises and the runner's backoff does the
                # work — see the module docstring's failure-vs-failure distinction.
                raise
            outcome = IngestOutcome(
                status="failed",
                reason="chunking_embedding",
                page_count=extracted.page_count,
                chunk_strategy=cutting.strategy,
            )
            await self._finish(
                scope,
                document_id,
                outcome,
                # Names the provider, not the splitter. A message reading "chunking
                # failed" sends somebody to read `chunking.py`, where there is nothing
                # wrong: under `semantic` this step embeds every sentence, and it is the
                # embedding call that broke.
                error=(
                    f"The '{cutting.strategy}' chunking strategy embeds every sentence to "
                    f"find topic boundaries, and the embedding provider refused. {error}"
                ),
                media_type=read.media_type,
                size_bytes=read.size_bytes,
                content_hash=read.content_hash,
            )
            return outcome

        if not chunks:
            outcome = IngestOutcome(
                status="skipped",
                error="This file contains no text to index.",
                reason="no_text",
                page_count=extracted.page_count,
            )
            # Whatever was indexed before must still go: an emptied file that keeps its
            # old chunks is the worst kind of stale, because retrieval still finds them.
            await self._vectors.delete_document(organization_id, document_id)
            await self._finish(
                scope,
                document_id,
                outcome,
                error=outcome.error,
                media_type=read.media_type,
                size_bytes=read.size_bytes,
                content_hash=read.content_hash,
            )
            return outcome

        async with self._store.begin(scope) as transaction:
            document = await self._require(transaction, document_id)
            name = document.source_name
            await self._advance(transaction, document, "embedding")

        # The two uses of a summary (task 102). Under `contextual` every source chunk is
        # embedded with the summary in front of it and returned without; under
        # `summary_chunk` the summary is one more point, labelled as one.
        if prefixes_context(summarizing.mode):
            chunks = [chunk.with_context(summarized.summary) for chunk in chunks]
        await self._index(
            organization_id=organization_id,
            connector_id=connector_id,
            document_id=document_id,
            source_name=name,
            source_uri=reference.key,
            content_hash=read.content_hash,
            chunks=chunks,
            config=cutting,
            cut=cut,
            tokenizer=tokenizer,
            summary=summarized.summary if adds_summary_chunk(summarizing.mode) else None,
        )

        outcome = IngestOutcome(
            status="indexed",
            chunk_count=len(chunks),
            page_count=extracted.page_count,
            chunk_strategy=cutting.strategy,
            chunk_fingerprint=cut,
            tokenizer=tokenizer.name,
        )
        await self._finish(
            scope,
            document_id,
            outcome,
            media_type=read.media_type,
            size_bytes=read.size_bytes,
            content_hash=read.content_hash,
        )
        logger.info(
            "document indexed",
            extra={
                "document_id": str(document_id),
                "chunks": len(chunks),
                "embedding_model": self._embedder.model,
                "chunk_strategy": cutting.strategy,
                "tokenizer": tokenizer.name,
                "summarization": summarizing.mode,
            },
        )
        return outcome

    # -- summarization (task 102) ----------------------------------------

    async def _summarize_phase(
        self,
        scope: TenantScope,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        source_name: str,
        text: str,
        content_hash: str,
        tokenizer: Tokenizer,
        config: SummarizationConfig,
        queue_name: str | None,
        allow_reuse: bool = True,
    ) -> _Summarized:
        """Decide the document's summary, and what its absence means.

        Three outcomes. The summary is *reused* from the row when the bytes have not
        changed and it was written by the same model under the same prompt, or by hand —
        so a chunking change does not buy the corpus a second round of model calls. It is
        *generated* otherwise. Or there is none, and the mode decides: under
        ``summary_chunk`` the document indexes without it and the row says why; under
        ``contextual`` a failure fails the document and the cap parks it, because half a
        corpus embedded with context and half without is two corpora that rank
        differently.
        """
        if config.mode == "off":
            return _Summarized(summary=None, model_id=None)

        model_id = await self._context_model(organization_id, config)
        async with self._store.begin(scope) as transaction:
            document = await self._require(transaction, document_id)
            if allow_reuse and _reusable(document, content_hash, model_id):
                await self._advance(transaction, document, "summarizing")
                return _Summarized(summary=document.summary, model_id=model_id)
            await self._advance(transaction, document, "summarizing")

        if self._summarizer is None:
            result = SummaryOutcome(
                status="failed",
                reason="no_summarization_model",
                error="This worker has no summarization model to call.",
            )
        else:
            result = await self._summarizer.summarize(
                organization_id=organization_id,
                connector_id=connector_id,
                document_id=document_id,
                source_name=source_name,
                text=text,
                tokenizer=tokenizer,
                config=config,
            )
        await self._store_summary(scope, document_id, result)

        if result.succeeded:
            return _Summarized(summary=result.summary, model_id=model_id)

        contextual = prefixes_context(config.mode)
        if result.status == CAPPED:
            if contextual:
                # Parked, not failed. The job comes back after midnight and finds the cap
                # reset; until then the row says what it is waiting for.
                await self._retry_after_midnight(
                    INGEST_DOCUMENT,
                    {"organization_id": str(organization_id), "document_id": str(document_id)},
                    key=f"{ingest_key(document_id, content_hash)}:cap",
                    queue_name=queue_name,
                )
                return _Summarized(
                    summary=None,
                    model_id=model_id,
                    outcome=IngestOutcome(
                        status="pending", reason=WAITING_ON_CAP, error=result.error
                    ),
                )
            # Indexed without a summary today, summarized tomorrow.
            await self._retry_after_midnight(
                SUMMARIZE_DOCUMENT,
                {"organization_id": str(organization_id), "document_id": str(document_id)},
                key=summarize_key(document_id, content_hash),
                queue_name=queue_name,
            )
            return _Summarized(summary=None, model_id=model_id)

        if contextual:
            return _Summarized(
                summary=None,
                model_id=model_id,
                outcome=IngestOutcome(
                    status="failed", reason=SUMMARIZATION_FAILED, error=result.error
                ),
            )
        return _Summarized(summary=None, model_id=model_id)

    async def _context_model(
        self, organization_id: uuid.UUID, config: SummarizationConfig
    ) -> uuid.UUID | None:
        """Which model the connector's settings resolve to, by id — the identity the
        ``contextual`` fingerprint carries. ``None`` when nothing is configured or when
        this process cannot resolve one."""
        choice = await self.summary_model(organization_id, config.model_id)
        return choice.id if choice is not None else None

    async def summary_model(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> ModelChoice | None:
        """The model a summarization setting resolves to, through the whole chain, or
        ``None``. Public so the connector screen and the staleness comparison use the
        resolver ingestion uses rather than one that agrees with it most of the time."""
        if self._summarizer is None:
            return None
        return await self._summarizer.models.describe(organization_id, model_id)

    @property
    def summarizes(self) -> bool:
        """Whether this process can call a summarization model at all."""
        return self._summarizer is not None

    async def _store_summary(
        self, scope: TenantScope, document_id: uuid.UUID, result: SummaryOutcome
    ) -> None:
        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                return
            document.summary = result.summary
            document.summary_status = result.status
            document.summary_error = result.error
            document.summary_model = result.model_name
            document.summary_model_id = result.model_id
            document.summary_prompt_version = result.prompt_version
            document.summary_tokens_in = result.tokens_in if result.succeeded else None
            document.summary_tokens_out = result.tokens_out if result.succeeded else None
            document.summarized_at = datetime.now(UTC) if result.succeeded else None
            await transaction.commit()

    async def _retry_after_midnight(
        self,
        name: str,
        payload: dict[str, str],
        *,
        key: str,
        queue_name: str | None,
    ) -> None:
        """Enqueue for the moment the daily cap resets. Keyed by the day it will run on,
        so a document parked twice in one day is one job."""
        now = datetime.now(UTC)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        delay = (tomorrow - now).total_seconds() + CAP_RETRY_MARGIN_SECONDS
        await self._queue.enqueue(
            JobRequest(
                name=name,
                payload=payload,
                idempotency_key=f"{key}:{tomorrow.date().isoformat()}",
                delay_seconds=delay,
                queue=queue_name,
            )
        )

    async def summarize(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID, regenerate: bool = False
    ) -> IngestOutcome | SummaryOutcome | None:
        """Just the summary phase, for a document that is already indexed.

        The **Summarize** retry, the morning after a cap hit, and the re-embed after an
        operator edits a summary. Under ``contextual`` there is no such thing as "just the
        summary" — every vector depends on it — so that case is the ordinary ingestion,
        which reuses a summary that is still valid and regenerates one that is not. Under
        ``summary_chunk`` it is one model call and one point.
        """
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise PermanentJobError(f"document {document_id} no longer exists")
            connector = await transaction.connector(document.connector_id)
            if connector is None or connector.status == "deleting":
                return None
            config = summarization_for(
                SummarizationConfig.load(connector.summarization),
                format_label(document.mime_type or ""),
            )
            connector_id = connector.id
            name = document.source_name
            indexed = document.status == "indexed"

        if config.mode == "off":
            await self._vectors.delete_points(
                organization_id, [point_id(document_id, SUMMARY_INDEX)]
            )
            return None
        if prefixes_context(config.mode) or not indexed:
            return await self.ingest(organization_id=organization_id, document_id=document_id)

        try:
            read = await self.read_document(
                organization_id=organization_id, document_id=document_id
            )
        except (NotFound, ExtractionError):
            # The document's own row already says why it cannot be read; a summary of
            # something unreadable is not a thing to retry for.
            return None
        tokenizer = self.tokenizer
        summarized = await self._summarize_phase(
            scope,
            organization_id=organization_id,
            connector_id=connector_id,
            document_id=document_id,
            source_name=name,
            text=read.extracted.text,
            content_hash=read.content_hash,
            tokenizer=tokenizer,
            config=config,
            queue_name=queue_for(name),
            allow_reuse=not regenerate,
        )
        async with self._store.begin(scope) as transaction:
            document = await self._require(transaction, document_id)
            await self._advance(transaction, document, "indexed")

        if summarized.summary is None:
            await self._vectors.delete_points(
                organization_id, [point_id(document_id, SUMMARY_INDEX)]
            )
            return None
        if adds_summary_chunk(config.mode):
            await self._vectors.ensure_collection(
                organization_id, dimension=self._embedder.dimension
            )
            await self._vectors.upsert(
                organization_id,
                [
                    self._summary_point(
                        organization_id=organization_id,
                        connector_id=connector_id,
                        document_id=document_id,
                        source_name=name,
                        source_uri=read.source_uri,
                        content_hash=read.content_hash,
                        summary=summarized.summary,
                        tokenizer=tokenizer,
                        vector=(await self._embedder.embed([summarized.summary]))[0],
                        ingested_at=datetime.now(UTC).isoformat(),
                    )
                ],
            )
        return None

    def _summary_point(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        source_name: str,
        source_uri: str,
        content_hash: str,
        summary: str,
        tokenizer: Tokenizer,
        vector: Sequence[float],
        ingested_at: str,
    ) -> ChunkPoint:
        """The one point per document that is *not* a quote, and says so.

        ``kind: summary`` is the label the prompt renderer, the citation resolver and the
        inspector all switch on. The rest of the payload is the ordinary shape, so every
        filter that works on a source chunk works on this one.
        """
        return ChunkPoint(
            id=point_id(document_id, SUMMARY_INDEX),
            vector=list(vector),
            payload={
                "org_id": str(organization_id),
                "connector_id": str(connector_id),
                "document_id": str(document_id),
                "source_name": source_name,
                "source_uri": source_uri,
                "page_or_section": SUMMARY_SECTION,
                "chunk_index": SUMMARY_INDEX,
                "ingested_at": ingested_at,
                "content_hash": content_hash,
                "token_count": token_estimate(summary, tokenizer),
                "text": summary,
                "kind": KIND_SUMMARY,
                "tokenizer": tokenizer.name,
            },
        )

    # -- chunking --------------------------------------------------------

    async def _chunk(
        self,
        extracted: Extracted,
        config: ChunkingConfig,
        *,
        media_type: str,
        embedder: Embedder | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> list[Chunk]:
        """Cut one document up, computing the boundary signal first where one is needed.

        This is the only place in the product that knows a chunking strategy can require
        network I/O, and keeping it here is the whole design:
        :func:`~app.services.chunking.chunk_document` stays a pure function of text and
        numbers, which is what lets every strategy be tested exhaustively without a fake
        embedder.

        The sentence spans go through the ordinary :class:`Embedder`, so they inherit its
        batching, its retries and its rate-limit backoff. Not doing that would mean a
        second, worse client on the same provider — and this call is the *larger* of the
        two a semantic document makes, since there are several sentences per chunk.
        """
        started = time.perf_counter()
        # The *given* embedder, defaulting to the serving one. A recut under a new model
        # has to find its boundaries with that model — using the serving one would make
        # the whole extra expense of the recut buy nothing.
        embedder = embedder or self._embedder
        with phase("ingestion.chunking", **{"chunking.strategy": config.strategy}) as span:
            try:
                signal: BoundarySignal | None = None
                if needs_signal(config):
                    request = plan_signal(extracted, config)
                    if span.is_recording():
                        span.set_attribute("chunking.signal_spans", len(request))
                        span.set_attribute("chunking.signal_stride", request.stride)
                    signal = boundary_signal(request, await embedder.embed(request.texts))
                chunks = chunk_document(
                    extracted,
                    config,
                    tokenizer=tokenizer or self.tokenizer,
                    media_type=media_type,
                    signal=signal,
                )
            except Exception as error:
                record_error(span, error)
                raise
            finally:
                self._observe_chunking(config.strategy, time.perf_counter() - started)
            if span.is_recording():
                span.set_attribute("chunking.chunks", len(chunks))

        if self._chunking_metrics is not None:
            sizes = self._chunking_metrics.sizes.labels(strategy=config.strategy)
            for chunk in chunks:
                sizes.observe(chunk.token_count)
        return chunks

    def _observe_chunking(self, strategy: str, seconds: float) -> None:
        if self._chunking_metrics is not None:
            self._chunking_metrics.duration.labels(strategy=strategy).observe(seconds)

    def _points(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        source_name: str,
        source_uri: str,
        content_hash: str,
        chunks: Sequence[Chunk],
        config: ChunkingConfig,
        cut: str,
        tokenizer: Tokenizer,
        vectors: Sequence[Sequence[float]],
        summary: str | None = None,
    ) -> list[ChunkPoint]:
        """Chunks and their vectors, as points. The one place a payload is built.

        Shared by ordinary ingestion and by :meth:`recut`, because a recut whose payload
        differed by one key from the ordinary path would produce a collection where half
        the chunks answer a filter and half do not — and it would look perfectly healthy
        until somebody searched with that filter.

        ``vectors`` has one entry per chunk, plus one more at the end when ``summary`` is
        given — the summary point's (task 102).
        """
        ingested_at = datetime.now(UTC).isoformat()
        source_vectors = vectors[: len(chunks)]
        points = [
            ChunkPoint(
                id=point_id(document_id, chunk.index),
                vector=list(vector),
                # SPEC §9.3's chunk metadata, in full. `text` rides along because task 10
                # needs the content and a second round trip per chunk would double the
                # latency of every augmented request.
                payload={
                    "org_id": str(organization_id),
                    "connector_id": str(connector_id),
                    "document_id": str(document_id),
                    "source_name": source_name,
                    "source_uri": source_uri,
                    "page_or_section": chunk.section,
                    "chunk_index": chunk.index,
                    "ingested_at": ingested_at,
                    "content_hash": content_hash,
                    "token_count": chunk.token_count,
                    "text": chunk.text,
                    "chunk_strategy": config.strategy,
                    "chunk_fingerprint": cut,
                    "tokenizer": tokenizer.name,
                    # Always present, so a reader never infers a kind from its absence.
                    "kind": KIND_SOURCE,
                    **_embedding_payload(chunk, config),
                },
            )
            for chunk, vector in zip(chunks, source_vectors, strict=True)
        ]
        if summary is not None:
            points.append(
                self._summary_point(
                    organization_id=organization_id,
                    connector_id=connector_id,
                    document_id=document_id,
                    source_name=source_name,
                    source_uri=source_uri,
                    content_hash=content_hash,
                    summary=summary,
                    tokenizer=tokenizer,
                    vector=vectors[len(chunks)],
                    ingested_at=ingested_at,
                )
            )
        return points

    async def _index(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        source_name: str,
        source_uri: str,
        content_hash: str,
        chunks: Sequence[Chunk],
        config: ChunkingConfig,
        cut: str,
        tokenizer: Tokenizer,
        summary: str | None = None,
    ) -> None:
        await self._vectors.ensure_collection(organization_id, dimension=self._embedder.dimension)
        # `vector_text`, not `text`. They are the same string under every strategy but
        # `sentence_window`, where the difference is the entire proposition — the sentence
        # is what a query is matched against and the window around it is what answers —
        # and under `contextual` summarization, where the summary rides in front.
        texts = [chunk.vector_text for chunk in chunks]
        if summary is not None:
            texts.append(summary)
        points = self._points(
            organization_id=organization_id,
            connector_id=connector_id,
            document_id=document_id,
            source_name=source_name,
            source_uri=source_uri,
            content_hash=content_hash,
            chunks=chunks,
            config=config,
            cut=cut,
            tokenizer=tokenizer,
            vectors=await self._embedder.embed(texts),
            summary=summary,
        )

        # Delete first, then upsert. Deterministic ids overwrite the points that still
        # exist; only a delete removes the tail of a document that got shorter.
        await self._vectors.delete_document(organization_id, document_id)
        await self._vectors.upsert(organization_id, points)

    # -- reading for a preview -------------------------------------------

    @property
    def tokenizer(self) -> Tokenizer:
        """The one this pipeline chunks with *now* — the embedding model's (task 101).
        Exposed so a preview measures chunks the same way ingestion does: two tokenizers
        would make the sizes on the comparison screen wrong by roughly a third, which is
        the gap between a BPE and the word-count fallback."""
        return self._tokenizers()

    async def read_document(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID, cap: int | None = None
    ) -> ReadDocument:
        """Fetch and extract one stored document, indexing nothing.

        Here rather than in the connector service because there must be exactly one way
        this product turns an object into :class:`~app.services.extraction.Extracted`. A
        second one would eventually disagree with this one, and the entire value of a
        chunking preview is that what it shows is what ingestion would do.

        Raises the same :class:`~app.services.extraction.ExtractionError` ingestion would,
        rather than translating it: the caller is a request handler and can turn it into a
        422 that says the same thing the document row would have said.
        """
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                raise NotFound("No such document.")
            connector = await transaction.connector(document.connector_id)
            if connector is None:  # pragma: no cover - foreign key makes this unreachable
                raise NotFound("No such document.")
            source = self._source(connector)
            reference = ObjectRef(
                key=document.source_uri, size_bytes=document.size_bytes, etag=document.etag
            )
            chunking = ChunkingConfig.load(connector.chunking)
            summarization = SummarizationConfig.load(connector.summarization)
            summary = document.summary
            summary_status = document.summary_status
            name = document.source_name
            connector_id = document.connector_id

        try:
            read = await self._read(source, reference, cap=cap)
        except KeyError as exc:
            raise NotFound("This file is no longer in storage. Run a resync.") from exc
        if read.outcome is not None:
            raise SkippedDocument(
                read.outcome.error or "This file cannot be read.",
                reason=read.outcome.reason or "unsupported_format",
            )
        extracted = await self._extract(read.data, name=reference.name, media_type=read.media_type)
        return ReadDocument(
            extracted=extracted,
            media_type=read.media_type,
            source_name=name,
            size_bytes=read.size_bytes,
            chunking=chunking,
            connector_id=connector_id,
            source_uri=reference.key,
            content_hash=read.content_hash,
            summarization=summarization,
            summary=summary,
            summary_status=summary_status,
        )

    async def recut(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID, embedder: Embedder
    ) -> list[ChunkPoint]:
        """Re-extract and re-chunk one document under a *different* embedding model.

        Task 17's platform reindex re-embeds stored chunk text, which is right for every
        strategy but one. Under ``semantic`` the boundaries themselves came from the old
        model, so re-embedding them faithfully reproduces the old model's opinion about
        where the topics changed — a reindex that costs the full price and moves the
        chunking not at all. This path pays the extra cost instead: back to object storage,
        extract again, cut again with the new model deciding the boundaries.

        Returns points and writes nothing. Where they go is the reindexer's business, and
        it is not the live collection: they belong in the one being built beside it.
        """
        read = await self.read_document(organization_id=organization_id, document_id=document_id)
        kind = format_label(read.media_type)
        config = effective(read.chunking, kind)
        summarizing = summarization_for(read.summarization, kind)
        tokenizer = self.tokenizer
        chunks = await self._chunk(
            read.extracted,
            config,
            media_type=read.media_type,
            embedder=embedder,
            tokenizer=tokenizer,
        )
        # The *stored* summary, not a fresh one: a recut is about the embedding model, and
        # the summary a document already has is as true under the new model as the old.
        summary = read.summary if read.summary_status == SUMMARIZED else None
        if prefixes_context(summarizing.mode):
            chunks = [chunk.with_context(summary) for chunk in chunks]
        texts = [chunk.vector_text for chunk in chunks]
        as_point = summary if adds_summary_chunk(summarizing.mode) else None
        if as_point is not None:
            texts.append(as_point)
        return self._points(
            organization_id=organization_id,
            connector_id=read.connector_id,
            document_id=document_id,
            source_name=read.source_name,
            source_uri=read.source_uri,
            content_hash=read.content_hash,
            chunks=chunks,
            config=config,
            cut=fingerprint(
                config,
                embedding_model=embedder.model,
                tokenizer=tokenizer.name,
                context=context_identity(
                    summarizing,
                    await self._context_model(organization_id, summarizing),
                    prompt_version=PROMPT_VERSION,
                ),
            ),
            tokenizer=tokenizer,
            vectors=await embedder.embed(texts),
            summary=as_point,
        )

    # -- reading ---------------------------------------------------------

    async def _read(
        self, source: ConnectorSource, reference: ObjectRef, *, cap: int | None = None
    ) -> _Read:
        """Stream the object, sniffing the type from the head and stopping early if it is
        one nothing here can read.

        ``cap`` overrides the per-file limit, and only the preview path uses it: comparing
        chunking strategies on a 50 MB export would spend a great deal at the embedding
        provider to fill a screen nobody can read.
        """
        digest = hashlib.sha256()
        buffer = bytearray()
        stream = source.fetch(reference)
        media_type: str | None = None
        cap = cap or self._settings.max_file_bytes

        try:
            async for piece in stream:
                digest.update(piece)
                buffer.extend(piece)

                if media_type is None and len(buffer) >= SNIFF_BYTES:
                    media_type = sniff(bytes(buffer[:SNIFF_BYTES]), name=reference.name)
                    verdict = self._unreadable(media_type, reference.name)
                    if verdict is not None:
                        return _Read(
                            data=b"",
                            media_type=media_type,
                            content_hash="",
                            size_bytes=reference.size_bytes,
                            outcome=verdict,
                        )

                if len(buffer) > cap:
                    return _Read(
                        data=b"",
                        media_type=media_type or "application/octet-stream",
                        content_hash="",
                        size_bytes=len(buffer),
                        outcome=IngestOutcome(
                            status="skipped",
                            error=(
                                f"This file is larger than the {cap // (1024 * 1024)} MB "
                                "per-file limit."
                            ),
                            reason="too_large",
                        ),
                    )
        finally:
            await _close(stream)

        data = bytes(buffer)
        if media_type is None:
            media_type = sniff(data[:SNIFF_BYTES], name=reference.name)
        verdict = self._unreadable(media_type, reference.name)
        return _Read(
            data=data,
            media_type=media_type,
            content_hash=digest.hexdigest(),
            size_bytes=len(data),
            outcome=verdict,
        )

    def _unreadable(self, media_type: str, name: str) -> IngestOutcome | None:
        """``None`` if something here can read it, otherwise the ``skipped`` outcome.

        The two messages are different on purpose. "Coming soon" is a roadmap statement a
        customer can wait on; "not supported" is a statement about the file. Collapsing
        them would make a folder of PDFs look like a folder of mistakes.
        """
        if self._registry.find(media_type=media_type, name=name) is not None:
            return None
        note = self._registry.pending_note(media_type)
        if note:
            return IngestOutcome(status="skipped", error=f"{note}.", reason="not_yet_supported")
        return IngestOutcome(
            status="skipped",
            error=f"This {describe(media_type)} is not a supported format.",
            reason="unsupported_format",
        )

    async def _extract(self, data: bytes, *, name: str, media_type: str) -> Extracted:
        found = self._registry.lookup(media_type=media_type, name=name)
        if found is None:  # pragma: no cover - `_unreadable` already returned
            raise ExtractionError("No extractor for this file type.")

        started = time.perf_counter()
        label = format_label(media_type)
        outcome = "ok"
        try:
            extracted = await self._run(found, data, name=name)
        except SkippedDocument:
            outcome = "skipped"
            raise
        except ExtractionError:
            outcome = "failed"
            raise
        finally:
            self._observe(label, outcome, time.perf_counter() - started)
        return extracted

    async def _run(self, found: Registration, data: bytes, *, name: str) -> Extracted:
        """One extraction, in whichever place this one is allowed to run.

        The isolated path owns its own clock, because it is the only one that can act on
        it: killing a subprocess actually stops the work, where a thread can only be
        abandoned. Both use the same configured number, so the isolation decision does not
        also change what "too long" means.
        """
        if found.isolation_key is not None and self._pool is not None:
            return await self._pool.run(found.isolation_key, data, name=name)
        try:
            async with asyncio.timeout(self._settings.extraction_timeout_seconds):
                # Off the event loop: extraction is CPU-bound, and a pathological regex
                # or a huge CSV would otherwise stall every other job on this worker.
                return await asyncio.to_thread(found.extractor, data, name=name)
        except TimeoutError as exc:
            # The thread is abandoned rather than killed — Python cannot interrupt one —
            # so this cap bounds the *job*, not the CPU. That is the whole reason the
            # binary formats are isolated instead: see `app.services.extraction_pool`.
            raise ExtractionError(
                "Reading this file took longer than "
                f"{int(self._settings.extraction_timeout_seconds)} seconds and was stopped.",
                reason="extraction_timeout",
            ) from exc

    def _observe(self, label: str, outcome: str, seconds: float) -> None:
        if self._metrics is None:
            return
        self._metrics.duration.labels(format=label).observe(seconds)
        self._metrics.completed.labels(format=label, outcome=outcome).inc()

    # -- resync ----------------------------------------------------------

    async def resync(self, *, organization_id: uuid.UUID, connector_id: uuid.UUID) -> ResyncSummary:
        """Reconcile the connector's documents against what the source actually holds.

        Two things protect this from an upload running at the same time, and the second is
        the one that matters. The lock stops two *resyncs* interleaving. It cannot help
        with an upload, because the race there opens before the lock is taken — the
        listing is a snapshot, and a file uploaded a millisecond later is legitimately
        absent from it. So a document is only ever deleted when it is in a **terminal**
        state: a row that says ``pending`` might be an upload whose object has not landed
        yet, and deleting it would erase a file the customer just added.
        """
        scope = TenantScope.of_organization(organization_id)
        async with self._lock.hold(resync_key(connector_id)) as held:
            if not held:
                logger.info(
                    "resync already running for this connector",
                    extra={"connector_id": str(connector_id)},
                )
                return ResyncSummary()
            return await self._reconcile(scope, organization_id, connector_id)

    async def _reconcile(
        self, scope: TenantScope, organization_id: uuid.UUID, connector_id: uuid.UUID
    ) -> ResyncSummary:
        outbox = JobOutbox(self._queue)
        added = updated = deleted = unchanged = skipped = 0

        async with self._store.begin(scope) as transaction:
            connector = await transaction.connector(connector_id)
            if connector is None:
                raise PermanentJobError(f"connector {connector_id} no longer exists")
            if connector.status == "deleting":
                return ResyncSummary()

            source = self._source(connector)
            known = {row[1]: row for row in await transaction.index(connector_id)}
            seen: set[str] = set()

            async for reference in source.list_objects():
                seen.add(reference.key)
                existing = known.get(reference.key)
                draft = DocumentDraft(
                    source_uri=reference.key,
                    source_name=reference.name,
                    size_bytes=reference.size_bytes,
                    etag=reference.etag,
                )

                if existing is None:
                    document = await transaction.claim_document(connector, draft, reset=True)
                    added += 1
                elif existing[3] not in TERMINAL_DOCUMENT_STATUSES:
                    # Something is already working on it, or something was and stopped.
                    # Either way the enqueue below is deduplicated by its key, so this is
                    # a recovery for the second case and a no-op for the first.
                    document = await transaction.claim_document(connector, draft, reset=False)
                    skipped += 1
                elif existing[2] != reference.etag:
                    document = await transaction.claim_document(connector, draft, reset=True)
                    updated += 1
                else:
                    unchanged += 1
                    continue

                outbox.add(
                    INGEST_DOCUMENT,
                    {"organization_id": str(organization_id), "document_id": str(document.id)},
                    idempotency_key=ingest_key(document.id, reference.etag),
                    queue=queue_for(reference.name),
                )

            for key, row in known.items():
                if key in seen or row[3] not in TERMINAL_DOCUMENT_STATUSES:
                    continue
                gone = await transaction.document(row[0])
                if gone is not None:
                    await self._vectors.delete_document(organization_id, gone.id)
                    await transaction.delete_document(gone)
                    deleted += 1

            connector.status = "ready"
            connector.error = None
            connector.last_synced_at = datetime.now(UTC)
            await transaction.commit()

        await outbox.flush()
        summary = ResyncSummary(
            added=added, updated=updated, deleted=deleted, unchanged=unchanged, skipped=skipped
        )
        logger.info(
            "connector resynced",
            extra={
                "connector_id": str(connector_id),
                "added": added,
                "updated": updated,
                "deleted": deleted,
                "unchanged": unchanged,
                "skipped": skipped,
            },
        )
        return summary

    # -- deletion --------------------------------------------------------

    async def purge(self, *, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        """Remove a connector's objects, vectors and rows, in that order.

        Bytes first, index second, rows last. Every other order leaves the failure mode
        that cannot be recovered from: without the row there is nothing left that knows
        the prefix or the vectors exist, and they are orphaned forever. This way a failure
        anywhere leaves the connector still marked ``deleting``, which the retry finds.
        """
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            connector = await transaction.connector(connector_id)
            if connector is None:
                return  # already gone; the delete asked for an end state that now holds
            prefix = connector.storage_prefix

        if prefix:
            removed = await self._objects.delete_prefix(prefix)
            logger.info(
                "removed connector objects",
                extra={"connector_id": str(connector_id), "objects": removed},
            )
        await self._vectors.delete_connector(organization_id, connector_id)

        async with self._store.begin(scope) as transaction:
            connector = await transaction.connector(connector_id)
            if connector is not None:
                # Attributed to the job, not to whoever pressed the button minutes ago:
                # the person asked for a deletion and that is already recorded; this is
                # the row, the objects and the vectors actually being gone, which the
                # worker is what knows.
                transaction.audit(
                    Attribution.system(organization_id, job="delete-connector"),
                    "connector.purge",
                    before=subject(connector),
                )
                await transaction.delete_connector(connector)
            await transaction.commit()

    async def drop_document(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID, delete_object: bool
    ) -> None:
        """Remove one document: its vectors, optionally its object, then its row."""
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                return
            key = document.source_uri

        await self._vectors.delete_document(organization_id, document_id)
        if delete_object:
            await self._objects.delete([key])

        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is not None:
                await transaction.delete_document(document)
            await transaction.commit()

    # -- helpers ---------------------------------------------------------

    def _source(self, connector: Connector) -> ConnectorSource:
        try:
            return build_source(
                type_=connector.type, store=self._objects, prefix=connector.storage_prefix
            )
        except ValueError as exc:
            raise PermanentJobError(str(exc)) from exc

    @staticmethod
    async def _require(transaction: object, document_id: uuid.UUID) -> Document:
        document = await transaction.document(document_id)  # type: ignore[attr-defined]
        if document is None:
            raise PermanentJobError(f"document {document_id} was deleted mid-ingestion")
        return document  # type: ignore[no-any-return]

    @staticmethod
    async def _advance(transaction: object, document: Document, status: str) -> None:
        document.status = status
        await transaction.commit()  # type: ignore[attr-defined]

    async def _finish(
        self,
        scope: TenantScope,
        document_id: uuid.UUID,
        outcome: IngestOutcome,
        *,
        error: str | None = None,
        media_type: str | None = None,
        size_bytes: int | None = None,
        content_hash: str | None = None,
    ) -> None:
        async with self._store.begin(scope) as transaction:
            document = await transaction.document(document_id)
            if document is None:
                return
            document.status = outcome.status
            document.error = error
            document.reason = outcome.reason
            document.chunk_count = outcome.chunk_count
            document.page_count = outcome.page_count
            if media_type is not None:
                document.mime_type = media_type
            if size_bytes is not None:
                document.size_bytes = size_bytes
            if content_hash:
                document.content_hash = content_hash
            document.chunk_strategy = outcome.chunk_strategy
            document.chunk_fingerprint = outcome.chunk_fingerprint
            document.tokenizer = outcome.tokenizer
            if outcome.status == "indexed":
                document.embedding_model = self._embedder.model
                document.indexed_at = datetime.now(UTC)
            else:
                document.indexed_at = None
                document.embedding_model = None
            await transaction.commit()


def _reusable(document: Document, content_hash: str, model_id: uuid.UUID | None) -> bool:
    """Whether the summary on the row is still the summary of *these* bytes under *this*
    configuration — so a chunking change does not buy a second round of model calls, and
    an operator's own summary survives a reindex."""
    if not document.summary or document.summary_status != SUMMARIZED:
        return False
    if document.content_hash != content_hash:
        return False
    if document.summary_model == MANUAL:
        return True
    return (
        document.summary_model_id == model_id and document.summary_prompt_version == PROMPT_VERSION
    )


def _embedding_payload(chunk: Chunk, config: ChunkingConfig) -> dict[str, Any]:
    """Why and how this chunk's vector differs from its text, or nothing at all.

    Task 20's ``embedded_text`` and ``window_sentences``, and task 102's ``context``, under
    one ``embedded_because`` that says which applies — because the chunk inspector
    highlights a matched sentence for one and shows a prefix for the other, and must not
    have to guess from which keys happen to be present.
    """
    payload = _window_payload(chunk, config)
    if chunk.contextual:
        payload["context"] = chunk.context
    if chunk.embedded_because is not None:
        payload["embedded_because"] = chunk.embedded_because
    return payload


def _window_payload(chunk: Chunk, config: ChunkingConfig) -> dict[str, Any]:
    """The two extra fields a windowed chunk needs, and nothing at all for the rest.

    ``embedded_text`` is what the chunk inspector highlights inside the window — without
    it, the first debugging session under ``sentence_window`` is "why does this chunk not
    contain the words I searched for", and the answer is not discoverable from the screen.

    ``window_sentences`` is read by retrieval's near-duplicate filter, which drops a chunk
    neighbouring one it already kept. The default radius of one is right for overlapping
    ``recursive`` chunks and far too narrow here: with a window of two, five consecutive
    chunks share text, and injecting all of them spends the token budget on one paragraph
    five times. Carried on the point rather than looked up from the connector because the
    filter runs on search results, and the configuration may have changed since.
    """
    if not chunk.windowed:
        return {}
    return {"embedded_text": chunk.embedded_text, "window_sentences": config.window_sentences}


async def _close(stream: AsyncIterator[bytes]) -> None:
    """Close a byte stream that was abandoned early.

    An async generator left half-consumed keeps its socket until the garbage collector
    gets to it, and the whole point of stopping at the sniff window is not to hold one.
    """
    closer = getattr(stream, "aclose", None)
    if closer is not None:
        await closer()


__all__ = [
    "DEFAULT_EXTRACTION_TIMEOUT_SECONDS",
    "DEFAULT_MAX_FILE_BYTES",
    "SUMMARIZATION_FAILED",
    "IngestOutcome",
    "IngestionPipeline",
    "IngestionSettings",
    "ResyncSummary",
]
