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
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.ids import uuid7
from app.core.metrics import ExtractionMetrics
from app.core.tenancy import Actor, TenantScope
from app.db.models import Connector, Document, Organization
from app.services.connector_source import storage_prefix
from app.services.connector_store import MemoryConnectorStore
from app.services.connectors import ConnectorService
from app.services.embeddings import HashEmbedder
from app.services.extraction import ExtractorRegistry, build_registry
from app.services.ingestion import IngestionPipeline, IngestionSettings
from app.services.job_queue import MemoryJobQueue
from app.services.jobs import (
    DELETE_CONNECTOR,
    INGEST_DOCUMENT,
    JobRunner,
    MemoryDeadLetters,
    RetryPolicy,
)
from app.services.locks import MemoryLock
from app.services.memory_db import MemoryDatabase
from app.services.object_store import MemoryObjectStore
from app.services.retrieval import MemoryService, Retriever
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
    embedder: HashEmbedder
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
    user_id: uuid.UUID = field(default_factory=uuid7)

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
) -> ConnectorFixture:
    settings = settings or get_settings()
    database = database or MemoryDatabase()
    store = MemoryConnectorStore(database)
    objects = objects or MemoryObjectStore()
    vectors = vectors or MemoryVectorStore()
    embedder = HashEmbedder(dimension=dimension, model="hash-bow")
    registry = build_registry()
    queue = MemoryJobQueue()
    lock = MemoryLock()
    limits = limits or IngestionSettings()

    pipeline = IngestionPipeline(
        store,
        objects=objects,
        vectors=vectors,
        embedder=embedder,
        tokenizer=TOKENIZER,
        registry=registry,
        queue=queue,
        lock=lock,
        settings=limits,
        # No `pool`: every extractor runs in a thread here. A subprocess pool costs a
        # second of interpreter startup per child and buys isolation that only
        # `tests/test_extraction_pool.py` is about.
        metrics=metrics,
    )
    service = ConnectorService(
        store,
        objects=objects,
        vectors=vectors,
        embedder=embedder,
        pipeline=pipeline,
        queue=queue,
        settings=limits,
    )

    retriever = Retriever(embedder, vectors)
    dead_letters = MemoryDeadLetters()

    # The same two handlers `app.workers.runtime.build_handlers` registers, spelled out
    # rather than imported: this module must not depend on the composition root, and the
    # bodies are one line each.
    async def ingest(payload: Mapping[str, Any]) -> None:
        await pipeline.ingest(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            document_id=uuid.UUID(str(payload["document_id"])),
        )

    async def purge(payload: Mapping[str, Any]) -> None:
        await pipeline.purge(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            connector_id=uuid.UUID(str(payload["connector_id"])),
        )

    runner = JobRunner(
        {INGEST_DOCUMENT: ingest, DELETE_CONNECTOR: purge},
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
    )


__all__ = [
    "DIMENSION",
    "TOKENIZER",
    "ConnectorFixture",
    "MemoryUpload",
    "build_connectors",
    "make_connector",
    "make_document",
]
