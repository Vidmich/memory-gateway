"""Building the background machinery, in one place, for two processes.

The API and the worker run the same objects for different reasons. The API needs the
ingestion pipeline because deleting a document and reconciling a connector happen on the
request path; the worker needs it because that is all it does. Assembling it twice would be
two wiring diagrams that drift, and the first symptom of the drift would be a worker
chunking differently from the endpoint that previewed the chunking.

Task 13 adds a second bundle for the same reason and with a sharper version of it.
:func:`build_distillation` is called by the worker, which *runs* passes, and by the API,
which arms them from the log flusher and runs one on demand when somebody presses "Distil
now". Those two paths have to agree about the dedupe threshold, the fact bound, the prompt
and the injection guard, or the button on the screen would produce different memory from the
background job — which is precisely the bug nobody would think to look for.

So this module builds both once from :class:`~app.core.clients.Clients` and
:class:`~app.core.config.Settings`, and both entry points call it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.clients import Clients
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.metrics import DistillationMetrics, ExtractionMetrics, JobMetrics
from app.services.catalog_store import PostgresCatalogStore
from app.services.connector_store import ConnectorStore, PostgresConnectorStore
from app.services.debounce import Debouncer, RedisDebouncer
from app.services.distillation_models import CatalogModelResolver
from app.services.distillation_store import DistillationStore, PostgresDistillationStore
from app.services.distillation_trigger import DistillationTrigger
from app.services.distiller import Distiller
from app.services.embeddings import Embedder, EmbeddingSettings, build_embedder
from app.services.end_user_store import EndUserStore, PostgresEndUserStore
from app.services.extraction import ExtractorRegistry, build_registry
from app.services.extraction_pool import ExtractionPool
from app.services.fact_vectors import FactVectorStore, QdrantFactVectorStore
from app.services.ingestion import IngestionPipeline, IngestionSettings
from app.services.job_queue import ArqJobQueue
from app.services.job_store import PostgresDeadLetters
from app.services.jobs import (
    DELETE_CONNECTOR,
    DISTIL_MEMORY,
    INGEST_DOCUMENT,
    DeadLetterSink,
    JobQueue,
    JobRunner,
    RetryPolicy,
)
from app.services.locks import Lock, RedisLock
from app.services.object_store import ObjectStore, S3ObjectStore
from app.services.proxy import ProxyService
from app.services.reconciliation import Reconciler
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
    #: The subprocess pool the heavy extractors run in. Lazy, so the API process — which
    #: builds all of this to delete documents and reconcile connectors — starts no
    #: children it will never use. Closed by whoever built it; see ``aclose`` below.
    pool: ExtractionPool

    async def aclose(self) -> None:
        await self.pool.aclose()


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


def build_ingestion(
    clients: Clients,
    settings: Settings,
    *,
    queue: JobQueue,
    metrics: ExtractionMetrics | None = None,
) -> Ingestion:
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
    pool = ExtractionPool(
        workers=settings.extraction_workers,
        # The same number the in-process path uses, so isolating a format does not also
        # change what "too long" means for it.
        timeout_seconds=settings.extraction_timeout_seconds,
        memory_limit_bytes=settings.extraction_memory_limit_bytes,
    )

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
            pool=pool,
            metrics=metrics,
        ),
        settings=limits,
        pool=pool,
    )


@dataclass(frozen=True, slots=True)
class Distillation:
    """Conversation memory's write half, already wired (SPEC §6.4).

    Holds both ends deliberately. :attr:`trigger` is what the API's log flusher calls to
    arm a pass; :attr:`distiller` is what the worker calls to run one — and what the
    control plane calls for "Distil now". One bundle rather than two because they share
    four objects, and a build that let them share three would be a screen whose button
    wrote memory the background job could not deduplicate against.
    """

    store: DistillationStore
    end_users: EndUserStore
    vectors: FactVectorStore
    debouncer: Debouncer
    #: The concrete resolver, not a protocol: it answers two different narrow ports — the
    #: worker's :class:`~app.services.distiller.ModelResolver` and the screen's
    #: :class:`~app.services.distillation_service.ModelDescriber` — and a bundle typed to
    #: one of them could not be handed to the other.
    models: CatalogModelResolver
    reconciler: Reconciler
    distiller: Distiller
    trigger: DistillationTrigger


def build_distillation(
    clients: Clients,
    settings: Settings,
    *,
    ingestion: Ingestion,
    metrics: DistillationMetrics | None = None,
) -> Distillation:
    """Everything conversation memory needs to write itself.

    Takes the ingestion bundle rather than rebuilding its parts: the embedder that indexes a
    fact has to be the embedder recall searches with, or the dedupe threshold is comparing
    numbers from two different vector spaces and the ``org_{id}_memory`` collection ends up
    holding both.
    """
    end_users = PostgresEndUserStore(clients.session_factory)
    fact_vectors = QdrantFactVectorStore(clients.qdrant)
    debouncer = RedisDebouncer(clients.redis)
    models = CatalogModelResolver(
        PostgresCatalogStore(clients.session_factory),
        secret_box=SecretBox.from_settings(settings),
        platform_default_id=settings.distillation_model_id,
    )
    reconciler = Reconciler(
        end_users,
        vectors=fact_vectors,
        embedder=ingestion.embedder,
        # The same Redis lock resync uses. One pass per person at a time; see
        # `app.services.reconciliation` for why it is a convenience rather than the
        # correctness mechanism.
        lock=ingestion.lock,
    )
    store = PostgresDistillationStore(clients.session_factory)
    return Distillation(
        store=store,
        end_users=end_users,
        vectors=fact_vectors,
        debouncer=debouncer,
        models=models,
        reconciler=reconciler,
        distiller=Distiller(
            store,
            end_users=end_users,
            reconciler=reconciler,
            models=models,
            # Its own client-facing service over the shared HTTP pool: the distillation
            # call is an ordinary chat completion, and routing it through the same object
            # the proxy uses means a provider quirk is fixed once.
            proxy=ProxyService(clients.http),
            debouncer=debouncer,
            metrics=metrics,
        ),
        trigger=DistillationTrigger(ingestion.queue, store=end_users, debouncer=debouncer),
    )


def retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.job_max_attempts,
        base_seconds=settings.job_backoff_base_seconds,
        cap_seconds=settings.job_backoff_cap_seconds,
    )


def build_handlers(
    ingestion: Ingestion, distillation: Distillation | None = None
) -> Mapping[str, Any]:
    """Job name to coroutine.

    Each one unpacks its payload and calls the service. Nothing else: the payload is
    strings because it crossed a process boundary, and turning strings back into ids is
    the only thing a handler is allowed to be.

    ``distillation`` is optional so that a deployment which has not enabled conversation
    memory runs a worker with two handlers rather than three. A ``distil_memory`` job
    arriving at such a worker is dead-lettered by name, which is a visible bad deploy
    rather than a silent one.
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

    handlers: dict[str, Any] = {
        INGEST_DOCUMENT: ingest_document,
        DELETE_CONNECTOR: delete_connector,
    }
    if distillation is None:
        return handlers

    async def distil_memory(payload: Mapping[str, Any]) -> None:
        session = payload.get("session_id")
        await distillation.distiller.run(
            organization_id=uuid.UUID(str(payload["organization_id"])),
            end_user_id=uuid.UUID(str(payload["end_user_id"])),
            session_id=str(session) if session is not None else None,
            # Present for a debounced pass, absent for a backfill. `None` means "run
            # whatever is pending"; a token means "run only if nothing newer was armed".
            token=str(payload["token"]) if payload.get("token") is not None else None,
        )

    handlers[DISTIL_MEMORY] = distil_memory
    return handlers


def build_runner(
    ingestion: Ingestion,
    settings: Settings,
    *,
    dead_letters: DeadLetterSink,
    metrics: JobMetrics | None = None,
    distillation: Distillation | None = None,
) -> JobRunner:
    return JobRunner(
        build_handlers(ingestion, distillation),
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
    "Distillation",
    "Ingestion",
    "build_dead_letters",
    "build_distillation",
    "build_handlers",
    "build_ingestion",
    "build_queue",
    "build_runner",
    "embedding_settings",
    "ingestion_settings",
    "retry_policy",
]
