"""Building the ingestion machinery, in one place, for two processes.

The API and the worker run the same objects for different reasons. The API needs the
pipeline because deleting a document and reconciling a connector happen on the request
path; the worker needs it because that is all it does. Assembling it twice would be two
wiring diagrams that drift, and the first symptom of the drift would be a worker chunking
differently from the endpoint that previewed the chunking.

So this module builds it once from :class:`~app.core.clients.Clients` and
:class:`~app.core.config.Settings`, and both entry points call it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.clients import Clients
from app.core.config import Settings
from app.core.metrics import JobMetrics
from app.services.connector_store import ConnectorStore, PostgresConnectorStore
from app.services.embeddings import Embedder, EmbeddingSettings, build_embedder
from app.services.extraction import ExtractorRegistry, build_registry
from app.services.ingestion import IngestionPipeline, IngestionSettings
from app.services.job_queue import ArqJobQueue
from app.services.job_store import PostgresDeadLetters
from app.services.jobs import (
    DELETE_CONNECTOR,
    INGEST_DOCUMENT,
    DeadLetterSink,
    JobQueue,
    JobRunner,
    RetryPolicy,
)
from app.services.locks import Lock, RedisLock
from app.services.object_store import ObjectStore, S3ObjectStore
from app.services.tokenizer import Tokenizer, build_tokenizer
from app.services.vector_store import QdrantVectorStore, VectorStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Ingestion:
    """Every part of the ingestion path, already wired."""

    store: ConnectorStore
    objects: ObjectStore
    vectors: VectorStore
    embedder: Embedder
    tokenizer: Tokenizer
    registry: ExtractorRegistry
    queue: JobQueue
    lock: Lock
    pipeline: IngestionPipeline
    settings: IngestionSettings


def embedding_settings(settings: Settings) -> EmbeddingSettings:
    return EmbeddingSettings(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        batch_size=settings.embedding_batch_size,
        max_concurrency=settings.embedding_max_concurrency,
    )


def ingestion_settings(settings: Settings) -> IngestionSettings:
    return IngestionSettings(
        max_file_bytes=settings.upload_max_file_bytes,
        extraction_timeout_seconds=settings.extraction_timeout_seconds,
        storage_quota_bytes=settings.storage_quota_bytes,
    )


def build_ingestion(clients: Clients, settings: Settings, *, queue: JobQueue) -> Ingestion:
    limits = ingestion_settings(settings)
    store = PostgresConnectorStore(clients.session_factory)
    objects = S3ObjectStore(clients.storage, clients.bucket)
    vectors = QdrantVectorStore(clients.qdrant)
    embedder = build_embedder(embedding_settings(settings), clients.http)
    # One tokenizer for the process. Loading the BPE vocabulary is expensive and the
    # object is stateless once loaded.
    tokenizer = build_tokenizer()
    registry = build_registry()
    lock = RedisLock(clients.redis)

    return Ingestion(
        store=store,
        objects=objects,
        vectors=vectors,
        embedder=embedder,
        tokenizer=tokenizer,
        registry=registry,
        queue=queue,
        lock=lock,
        pipeline=IngestionPipeline(
            store,
            objects=objects,
            vectors=vectors,
            embedder=embedder,
            tokenizer=tokenizer,
            registry=registry,
            queue=queue,
            lock=lock,
            settings=limits,
        ),
        settings=limits,
    )


def retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.job_max_attempts,
        base_seconds=settings.job_backoff_base_seconds,
        cap_seconds=settings.job_backoff_cap_seconds,
    )


def build_handlers(ingestion: Ingestion) -> Mapping[str, Any]:
    """Job name to coroutine.

    Each one unpacks its payload and calls the pipeline. Nothing else: the payload is
    strings because it crossed a process boundary, and turning strings back into ids is
    the only thing a handler is allowed to be.
    """
    import uuid

    async def ingest_document(payload: Mapping[str, Any]) -> None:
        await ingestion.pipeline.ingest(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            document_id=uuid.UUID(str(payload["document_id"])),
        )

    async def delete_connector(payload: Mapping[str, Any]) -> None:
        await ingestion.pipeline.purge(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            connector_id=uuid.UUID(str(payload["connector_id"])),
        )

    return {INGEST_DOCUMENT: ingest_document, DELETE_CONNECTOR: delete_connector}


def build_runner(
    ingestion: Ingestion,
    settings: Settings,
    *,
    dead_letters: DeadLetterSink,
    metrics: JobMetrics | None = None,
) -> JobRunner:
    return JobRunner(
        build_handlers(ingestion),
        queue=ingestion.queue,
        dead_letters=dead_letters,
        policy=retry_policy(settings),
        metrics=metrics,
    )


def build_queue(pool: Any) -> ArqJobQueue:
    return ArqJobQueue(pool)


def build_dead_letters(clients: Clients) -> PostgresDeadLetters:
    return PostgresDeadLetters(clients.session_factory)


__all__ = [
    "Ingestion",
    "build_dead_letters",
    "build_handlers",
    "build_ingestion",
    "build_queue",
    "build_runner",
    "embedding_settings",
    "ingestion_settings",
    "retry_policy",
]
