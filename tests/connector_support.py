"""Builders for connector and ingestion tests.

Everything is the *second* implementation of a port rather than a mock: the memory
object store, the memory vector store, the in-process queue, and the hashing embedder.
That is the difference that matters — a test here exercises the same
:class:`~app.services.ingestion.IngestionPipeline` a worker runs, all the way to a chunk
being searchable, and the only things swapped out are the four sockets.

``tests/object_store_contract.py`` and ``tests/vector_store_contract.py`` run the same
assertions against MinIO and Qdrant, which is what keeps that claim true.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.core.metrics import ChunkingMetrics, ExtractionMetrics, SummarizationMetrics
from app.core.tenancy import Actor, TenantScope
from app.db.models import Connector, Document, Organization, UpstreamModel
from app.schemas.openai import ChatResponse, Choice, ResponseMessage, Usage
from app.services.catalog_store import MemoryCatalogStore
from app.services.connector_source import storage_prefix
from app.services.connector_store import MemoryConnectorStore
from app.services.connectors import ConnectorService
from app.services.distillation_models import CatalogModelResolver
from app.services.embeddings import Embedder, HashEmbedder
from app.services.end_user_store import MemoryEndUserStore
from app.services.extraction import ExtractorRegistry, build_registry
from app.services.ingestion import IngestionPipeline, IngestionSettings
from app.services.job_queue import MemoryJobQueue
from app.services.jobs import (
    DELETE_CONNECTOR,
    INGEST_DOCUMENT,
    SUMMARIZE_DOCUMENT,
    JobRequest,
    JobRunner,
    MemoryDeadLetters,
    RetryPolicy,
)
from app.services.locks import MemoryLock
from app.services.memory_db import MemoryDatabase
from app.services.object_store import MemoryObjectStore
from app.services.proxy import Prepared
from app.services.reprocessing import CachedFingerprints, Reprocessor
from app.services.reprocessing_store import MemoryReprocessingStore
from app.services.retrieval import MemoryService, Retriever
from app.services.summarization_store import MemorySummarizationStore
from app.services.summarizer import SummarizationModelResolver, Summarizer
from app.services.tokenizer import Tokenizer, WordTokenizer
from app.services.vector_store import MemoryVectorStore

#: The word tokenizer, not tiktoken. Deterministic and offline, so a chunking assertion
#: is about the splitter rather than about a vocabulary downloaded at test time.
TOKENIZER: Tokenizer = WordTokenizer()

#: Width of the test embedder. Small because most tests index a handful of chunks and a
#: narrow vector keeps the arithmetic cheap. A test with a corpus of a few hundred chunks
#: should ask for more — the local embedder hashes words into buckets, and at 64 buckets a
#: few hundred documents collide often enough that ranking stops being about the text.
DIMENSION = 64


class ScriptedSummaryModel:
    """A :class:`~app.services.summarizer.Completer` that answers from a queue (task 102).

    Replies are consumed in order and the last one repeats, like the distillation double.
    A queued :class:`Exception` is raised instead of returned. ``usage`` is what the
    provider reports; ``None`` reports nothing, which is how the estimate path is reached.
    """

    def __init__(self, *replies: str | Exception, usage: tuple[int, int] | None = (120, 40)):
        self.replies: list[str | Exception] = list(replies) or ["A summary of the document."]
        self.usage = usage
        self.requests: list[Prepared] = []

    def queue(self, *replies: str | Exception) -> None:
        self.replies.extend(replies)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last_prompt(self) -> str:
        prepared = self.requests[-1]
        return "\n".join(str(message.content or "") for message in prepared.request.messages)

    async def complete(self, prepared: Prepared) -> ChatResponse:
        self.requests.append(prepared)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        usage = (
            Usage(
                prompt_tokens=self.usage[0],
                completion_tokens=self.usage[1],
                total_tokens=sum(self.usage),
            )
            if self.usage is not None
            else None
        )
        return ChatResponse(
            id="chatcmpl-summary",
            model=prepared.target.upstream_model_id,
            choices=[Choice(index=0, message=ResponseMessage(role="assistant", content=reply))],
            usage=usage,
        )


def make_summary_model(name: str = "cheap-summarizer") -> UpstreamModel:
    """A global, credential-less model row for the summarizer to resolve to."""
    return UpstreamModel(
        id=uuid7(),
        organization_id=None,
        scope="global",
        name=name,
        description=None,
        base_url="https://api.example.com/v1",
        dialect="openai",
        upstream_model_id="gpt-4o-mini",
        auth_type="none",
        extra_headers={},
        system_context=None,
        default_params={},
        timeout_seconds=30,
        enabled=True,
    )


def park_delayed(queue: MemoryJobQueue) -> list[JobRequest]:
    """Take the jobs waiting on a delay out of the queue, and return them.

    The memory queue ignores ``delay_seconds`` — a drain would run a cap retry
    immediately, find the cap still spent, and park the document again forever — so a
    test that has parked something takes the delayed jobs out first and asserts on them.
    """
    delayed = [job for job in queue.pending if job.delay_seconds > 0]
    queue.pending[:] = [job for job in queue.pending if job.delay_seconds <= 0]
    return delayed


class MemoryUpload:
    """One file, in the shape :class:`~app.services.connectors.UploadedFile` expects.

    Reads in pieces, exactly as Starlette's ``UploadFile`` does, so an upload path that
    only works because the whole body arrived in one call fails here too.
    """

    def __init__(self, filename: str, data: bytes, *, chunk: int = 64 * 1024) -> None:
        self.filename: str | None = filename
        self._data = data
        self._chunk = chunk
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        limit = self._chunk if size < 0 else min(size, self._chunk)
        piece = self._data[self._offset : self._offset + limit]
        self._offset += len(piece)
        return piece


@dataclass
class ConnectorFixture:
    """A whole ingestion stack over memory, plus the rows it was built around."""

    settings: Settings
    database: MemoryDatabase
    store: MemoryConnectorStore
    objects: MemoryObjectStore
    vectors: MemoryVectorStore
    embedder: Embedder
    registry: ExtractorRegistry
    queue: MemoryJobQueue
    lock: MemoryLock
    dead_letters: MemoryDeadLetters
    pipeline: IngestionPipeline
    service: ConnectorService
    #: Task 10, over the *same* vector store and the same embedder ingestion writes with.
    #: That shared object is the point: a retrieval test here searches the index a real
    #: upload in the same test actually built, rather than a fixture that agrees with it.
    memory: MemoryService
    runner: JobRunner
    organization: Organization
    connector: Connector
    #: Task 102. The ledger, the model chain, the scripted model, and the row it resolves
    #: to — a global model that is also the platform default, so a connector that names
    #: nothing still finds one.
    summaries: MemorySummarizationStore
    summary_models: SummarizationModelResolver
    summary_model: ScriptedSummaryModel
    summary_row: UpstreamModel
    #: Task 104. The runs, over the same store and queue; and the fingerprint source
    #: retrieval labels stale chunks with.
    reprocessor: Reprocessor
    fingerprints: CachedFingerprints
    user_id: uuid.UUID = field(default_factory=uuid7)
    #: Jobs :meth:`run_jobs_until_parked` set aside because they were waiting on a delay.
    parked: list[JobRequest] = field(default_factory=list)

    @property
    def actor(self) -> Actor:
        return Actor(
            user_id=self.user_id,
            scope=TenantScope(role="org_admin", organization_id=self.organization.id),
        )

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    async def put(self, name: str, data: bytes, *, connector: Connector | None = None) -> str:
        """Place an object under a connector's prefix without going through upload.

        This is what a presigned PUT looks like from the service's point of view: bytes
        appear in storage, and nothing has told the database about them until a resync.
        """
        target = connector or self.connector
        key = f"{target.storage_prefix}{name}"
        await self.objects.put(key, _stream(data))
        return key

    async def upload(self, *files: tuple[str, bytes], connector: Connector | None = None) -> None:
        target = connector or self.connector
        await self.service.upload(
            self.actor, target.id, [MemoryUpload(name, data) for name, data in files]
        )

    async def run_jobs(self) -> int:
        """Drain the queue, exactly as a worker would."""
        return await self.queue.drain(self.runner)

    async def run_jobs_until_parked(self) -> int:
        """Drain the queue, setting aside the jobs waiting on a delay (task 102's cap
        retries), which a real queue would hold until tomorrow and this one would run at
        once — see :func:`park_delayed`."""
        done = 0
        while True:
            self.parked.extend(park_delayed(self.queue))
            if not self.queue.pending:
                return done
            await self.runner.run(self.queue.pending.pop(0))
            done += 1

    async def summaries_health(self, *, connector_id: uuid.UUID | None = None) -> Any:
        """The Monitoring panel's block over the last day, straight from the ledger."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        async with self.summaries.begin(TenantScope.of_organization(self.organization_id)) as tx:
            return await tx.health(
                start=now - timedelta(days=1),
                end=now + timedelta(minutes=1),
                connector_id=connector_id,
            )

    async def ingest(self, *files: tuple[str, bytes]) -> None:
        """Upload and index, the common two-step of most tests here."""
        await self.upload(*files)
        await self.run_jobs()

    async def documents(self, connector: Connector | None = None) -> Sequence[Document]:
        target = connector or self.connector
        page = await self.service.list_documents(self.actor, target.id, limit=200)
        return page.items

    async def document(self, name: str) -> Document:
        for document in await self.documents():
            if document.source_name == name:
                return document
        raise AssertionError(f"no document called {name!r}")

    async def statuses(self) -> dict[str, str]:
        return {document.source_name: document.status for document in await self.documents()}

    async def chunk_count(self, *, document_id: uuid.UUID | None = None) -> int:
        return await self.vectors.count(
            self.organization_id, connector_id=self.connector.id, document_id=document_id
        )

    async def configure_summarization(self, **values: Any) -> None:
        """Set the connector's summarization section, as the panel's PATCH would."""
        from app.services.connectors import ConnectorPatch

        await self.service.update_connector(
            self.actor, self.connector.id, ConnectorPatch(summarization=values)
        )

    def runs(self) -> list[Any]:
        """Every ledger row, oldest first."""
        return sorted(self.database.summarization_runs.values(), key=lambda row: row.created_at)

    async def points(self, document_id: uuid.UUID) -> list[Any]:
        """A document's stored points, in cut order — the summary point first."""
        return await self.vectors.chunks(self.organization_id, document_id)


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


def make_connector(
    organization: Organization,
    *,
    name: str = "Product docs",
    chunking: dict[str, object] | None = None,
) -> Connector:
    connector = Connector(
        id=uuid7(),
        organization_id=organization.id,
        name=name,
        type="managed_file_drop",
        chunking=chunking or {},
        status="ready",
    )
    connector.storage_prefix = storage_prefix(
        organization_id=organization.id, connector_id=connector.id
    )
    return connector


def make_document(
    connector: Connector,
    *,
    name: str = "handbook.md",
    status: str = "indexed",
    chunk_count: int = 3,
) -> Document:
    """A document row without running the pipeline, for tests that only need one to
    exist — the cross-tenant net, above all, which needs a *foreign* id that is real."""
    return Document(
        id=uuid7(),
        organization_id=connector.organization_id,
        connector_id=connector.id,
        source_uri=f"{connector.storage_prefix}{name}",
        source_name=name,
        mime_type="text/markdown",
        size_bytes=128,
        status=status,
        chunk_count=chunk_count,
        index_status="current",
    )


def build_connectors(
    organization: Organization,
    *,
    database: MemoryDatabase | None = None,
    settings: Settings | None = None,
    objects: MemoryObjectStore | None = None,
    vectors: MemoryVectorStore | None = None,
    limits: IngestionSettings | None = None,
    connector: Connector | None = None,
    dimension: int = DIMENSION,
    metrics: ExtractionMetrics | None = None,
    chunking_metrics: ChunkingMetrics | None = None,
    #: Swapped in by the tests that need the provider to misbehave — task 20 made chunking
    #: a step that can call one, so "the embedding provider is down" is now a thing that
    #: happens *before* a document is indexed as well as during.
    embedder: Embedder | None = None,
    #: Task 101: a tokenizer, or a function returning the current one, for the tests that
    #: move it under a running pipeline. Defaults to the word tokenizer like everything.
    tokenizer: Tokenizer | Callable[[], Tokenizer] | None = None,
    #: Task 102: the scripted summarization model, and its metrics.
    summary_model: ScriptedSummaryModel | None = None,
    summarization_metrics: SummarizationMetrics | None = None,
) -> ConnectorFixture:
    settings = settings or get_settings()
    database = database or MemoryDatabase()
    store = MemoryConnectorStore(database)
    objects = objects or MemoryObjectStore()
    vectors = vectors or MemoryVectorStore()
    embedder = embedder or HashEmbedder(dimension=dimension, model="hash-bow")
    registry = build_registry()
    queue = MemoryJobQueue()
    lock = MemoryLock()
    limits = limits or IngestionSettings()

    # Task 102. A global model row that is also the platform default, so a connector
    # that names no model still resolves one through the whole chain; the scripted
    # completer stands in for the provider. The row exists only when a test asked for a
    # model — every other test's catalog stays exactly what it seeded.
    summary_row = make_summary_model()
    if summary_model is not None:
        database.add_model(summary_row)
    summaries = MemorySummarizationStore(database)
    summary_models = SummarizationModelResolver(
        CatalogModelResolver(
            MemoryCatalogStore(database),
            secret_box=SecretBox.from_settings(settings),
            platform_default_id=summary_row.id,
        ),
        settings=MemoryEndUserStore(database),
    )
    scripted = summary_model or ScriptedSummaryModel()
    summarizer = Summarizer(
        summaries, models=summary_models, proxy=scripted, metrics=summarization_metrics
    )

    pipeline = IngestionPipeline(
        store,
        objects=objects,
        vectors=vectors,
        embedder=embedder,
        tokenizer=tokenizer or TOKENIZER,
        registry=registry,
        queue=queue,
        lock=lock,
        settings=limits,
        # No `pool`: every extractor runs in a thread here. A subprocess pool costs a
        # second of interpreter startup per child and buys isolation that only
        # `tests/test_extraction_pool.py` is about.
        metrics=metrics,
        chunking_metrics=chunking_metrics,
        summarizer=summarizer,
    )
    reprocessor = Reprocessor(
        MemoryReprocessingStore(database),
        connectors=store,
        expected=pipeline.expected_fingerprints,
        queue=queue,
    )
    service = ConnectorService(
        store,
        objects=objects,
        vectors=vectors,
        embedder=embedder,
        pipeline=pipeline,
        queue=queue,
        settings=limits,
        reprocessor=reprocessor,
    )

    # No TTL: a test that changes a setting and retrieves in the next line wants the
    # label to follow the change, and the cache is about request-path cost, not truth.
    fingerprints = CachedFingerprints(store, expected=pipeline.expected_fingerprints, ttl_seconds=0)
    retriever = Retriever(embedder, vectors, fingerprints=fingerprints)
    dead_letters = MemoryDeadLetters()

    # The same two handlers `app.workers.runtime.build_handlers` registers, spelled out
    # rather than imported: this module must not depend on the composition root, and the
    # bodies are one line each.
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

    runner = JobRunner(
        {INGEST_DOCUMENT: ingest, DELETE_CONNECTOR: purge, SUMMARIZE_DOCUMENT: summarize},
        queue=queue,
        dead_letters=dead_letters,
        # No jitter in tests: a retry schedule asserted against a random draw is a test
        # that fails one run in twenty for no reason.
        policy=RetryPolicy(jitter=lambda _, ceiling: ceiling),
    )

    row = connector or make_connector(organization)
    database.add_connector(row)

    return ConnectorFixture(
        settings=settings,
        database=database,
        store=store,
        objects=objects,
        vectors=vectors,
        embedder=embedder,
        registry=registry,
        queue=queue,
        lock=lock,
        dead_letters=dead_letters,
        pipeline=pipeline,
        service=service,
        memory=MemoryService(retriever),
        runner=runner,
        organization=organization,
        connector=row,
        summaries=summaries,
        summary_models=summary_models,
        summary_model=scripted,
        summary_row=summary_row,
        reprocessor=reprocessor,
        fingerprints=fingerprints,
    )


__all__ = [
    "DIMENSION",
    "TOKENIZER",
    "ConnectorFixture",
    "MemoryUpload",
    "ScriptedSummaryModel",
    "build_connectors",
    "make_connector",
    "make_document",
    "make_summary_model",
    "park_delayed",
]
