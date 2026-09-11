"""A whole platform layer over in-memory stores.

Task 17's jobs read four things at once — PostgreSQL, Qdrant, the object store and the
platform settings — and the interesting assertions are all *across* them: a fact whose row
went and whose vector did not, a collection that has points and no documents, a partition
that was dropped while another gateway still had a claim on the day. Building that by hand
per test would be forty lines of wiring before the first assertion, and the wiring is what
would drift.

So this is one fixture with everything already connected to everything, and every store
shared with the one the rest of the suite already uses. That sharing is the point: a
retention test seeds a request log through :class:`~tests.monitoring_support.LogFixture`
and this reads the same ``MemoryDatabase``, so what it prunes is what the monitoring screen
would have shown.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.config import Settings, get_settings
from app.schemas.platform import EmbeddingChoice
from app.services.embeddings import Embedder, HashEmbedder
from app.services.end_user_store import MemoryEndUserStore
from app.services.erasure import OrganizationEraser
from app.services.fact_vectors import FactPoint, MemoryFactVectorStore, fact_payload
from app.services.jobs import JobRequest
from app.services.maintenance import OrphanSweeper, PartitionManager, RetentionJob
from app.services.maintenance_store import MemoryMaintenanceStore
from app.services.memory_db import MemoryDatabase
from app.services.object_store import MemoryObjectStore
from app.services.platform_service import PlatformService
from app.services.platform_settings import PlatformSettingsService
from app.services.platform_store import MemoryPlatformSettingsStore
from app.services.reindex import Recutter, Reindexer
from app.services.reindex_store import MemoryReindexStore
from app.services.vector_backends import VectorBackends, single_backend
from app.services.vector_index import MemoryVectorIndexAdmin
from app.services.vector_migration import VectorMigrator
from app.services.vector_store import ChunkPoint, MemoryVectorStore, point_id

#: The width the fixture's embedder produces. Small; nothing here measures embedding
#: quality, only that the same vectors go in and come out of the same index.
DIMENSION = 64


@dataclass
class RecordingQueue:
    """A job queue that keeps what it was handed.

    Used rather than running the reindex inline, because "the API records a run and
    enqueues it" and "the worker does the work" are two assertions and folding them into
    one would hide a missing enqueue behind a passing end-to-end test.
    """

    submitted: list[JobRequest] = field(default_factory=list)

    async def enqueue(self, request: JobRequest) -> str | None:
        self.submitted.append(request)
        return str(uuid.uuid4())

    async def depth(self) -> int:
        return len(self.submitted)

    async def ping(self) -> None:
        return None


@dataclass
class PlatformFixture:
    """Every part of task 17, wired, over one in-memory database."""

    db: MemoryDatabase
    settings: Settings
    store: MemoryMaintenanceStore
    vectors: MemoryVectorStore
    index: MemoryVectorIndexAdmin
    backends: VectorBackends
    facts: MemoryFactVectorStore
    objects: MemoryObjectStore
    embedder: Embedder
    settings_store: MemoryPlatformSettingsStore
    platform_settings: PlatformSettingsService
    partitions: PartitionManager
    retention: RetentionJob
    sweeper: OrphanSweeper
    reindexer: Reindexer
    migrator: VectorMigrator
    eraser: OrganizationEraser
    queue: RecordingQueue
    service: PlatformService

    # -- seeding ---------------------------------------------------------

    async def index_chunks(
        self,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
        *texts: str,
        connector_id: uuid.UUID | None = None,
    ) -> list[str]:
        """Put a document's chunks in the live collection, the way ingestion would."""
        await self.vectors.ensure_collection(organization_id, dimension=DIMENSION)
        vectors = await self.embedder.embed(list(texts))
        points = [
            ChunkPoint(
                id=point_id(document_id, position),
                vector=list(vector),
                payload={
                    "text": text,
                    "document_id": str(document_id),
                    "connector_id": str(connector_id or document_id),
                    "chunk_index": position,
                },
            )
            for position, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ]
        await self.vectors.upsert(organization_id, points)
        return [point.id for point in points]

    async def index_fact(
        self, organization_id: uuid.UUID, end_user_id: uuid.UUID, fact_id: uuid.UUID, text: str
    ) -> None:
        await self.facts.ensure_collection(organization_id, dimension=DIMENSION)
        vector = (await self.embedder.embed([text]))[0]
        await self.facts.upsert(
            organization_id,
            [
                FactPoint(
                    id=str(fact_id),
                    vector=list(vector),
                    payload=fact_payload(
                        organization_id=organization_id,
                        end_user_id=end_user_id,
                        kind="fact",
                        confidence=1.0,
                        created_at=datetime.now(UTC).timestamp(),
                    ),
                )
            ],
        )

    def partition_days(self, table: str, days: Iterable[date]) -> None:
        """Declare which daily partitions exist. See the memory store on why a set is all
        the policy layer ever asks of them."""
        self.store.partitions[table] = set(days)

    def with_partitions_around(self, *, before: int = 40, after: int = 30) -> None:
        today = datetime.now(UTC).date()
        days = {today + timedelta(days=offset) for offset in range(-before, after + 1)}
        for table in ("request_logs", "transcripts"):
            self.partition_days(table, days)

    async def configure(self, section: str, value: dict[str, Any]) -> None:
        """Write one settings section directly, skipping the API's validation.

        For tests that need a platform already configured rather than tests *of* the
        configuring, which go through the service so the audit event fires.
        """
        async with self.settings_store.begin() as transaction:
            await transaction.put(section, value, updated_by=None)
            await transaction.commit()
        await self.platform_settings.refresh()


def build_platform(
    db: MemoryDatabase | None = None,
    *,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    pause_seconds: float = 0.0,
    #: Task 20. ``None`` is the shape every process but the worker has, and the reindexer
    #: is expected to refuse a run that needs one rather than quietly copy instead.
    recutter: Recutter | None = None,
    #: Task 104. The per-connector runs a recut leaves behind; ``None`` is untracked.
    tracker: Any = None,
) -> PlatformFixture:
    """One call, everything connected.

    ``pause_seconds`` defaults to zero here and to fifty milliseconds in production. The
    pause exists so pruning cannot crowd out serving; a test asserting on what was deleted
    is not serving anything, and paying it per ``(gateway, day)`` would make the retention
    tests take seconds for no signal.
    """
    database = db if db is not None else MemoryDatabase()
    resolved = settings or get_settings()
    store = MemoryMaintenanceStore(database)
    vectors = MemoryVectorStore()
    index = MemoryVectorIndexAdmin(vectors)
    facts = MemoryFactVectorStore()
    # One backend, no database. The routing seam is exercised where it matters — in
    # `tests/test_vector_backends.py` — and every other platform test is about retention,
    # reindexing and sweeping rather than about where the vectors happen to live.
    backends = single_backend(store=vectors, facts=facts, admin=index)
    objects = MemoryObjectStore()
    embed = embedder or HashEmbedder(dimension=DIMENSION, model="hash-bow")
    queue = RecordingQueue()

    settings_store = MemoryPlatformSettingsStore(database)
    platform_settings = PlatformSettingsService(
        settings_store,
        settings=resolved,
        # Never in a test: the refresher is a background task, and one ticking underneath
        # an assertion is a source of flakes rather than of coverage.
        refresh_seconds=3600.0,
    )
    partitions = PartitionManager(store)
    retention = RetentionJob(store, partitions=partitions, facts=facts, pause_seconds=pause_seconds)
    sweeper = OrphanSweeper(store, vectors=vectors, backends=backends, facts=facts, objects=objects)
    reindexer = Reindexer(
        MemoryReindexStore(database),
        backends=backends,
        maintenance=store,
        settings=platform_settings,
        embedder_for=lambda choice: _embedder_for(choice, embed),
        recutter=recutter,
        pause_seconds=0.0,
        tracker=tracker,
    )
    migrator = VectorMigrator(
        backends, end_users=MemoryEndUserStore(database), embedder=embed, queue=queue
    )
    eraser = OrganizationEraser(
        store, vectors=vectors, facts=facts, objects=objects, backends=backends
    )

    return PlatformFixture(
        db=database,
        settings=resolved,
        store=store,
        vectors=vectors,
        index=index,
        backends=backends,
        facts=facts,
        objects=objects,
        embedder=embed,
        settings_store=settings_store,
        platform_settings=platform_settings,
        partitions=partitions,
        retention=retention,
        sweeper=sweeper,
        reindexer=reindexer,
        migrator=migrator,
        eraser=eraser,
        queue=queue,
        service=PlatformService(
            settings=platform_settings,
            partitions=partitions,
            retention=retention,
            sweeper=sweeper,
            reindexer=reindexer,
            eraser=eraser,
            store=store,
            backends=backends,
            migrator=migrator,
            queue=queue,
        ),
    )


def _embedder_for(choice: EmbeddingChoice, fallback: Embedder) -> Embedder:
    """The embedder a reindex would build for a target model.

    A *different* one for a different width, which is what makes the dimension-change test
    real: a fixture that handed back the same embedder whatever it was asked for would
    verify a collection against vectors it was already full of.
    """
    if choice.dimension == fallback.dimension and choice.name == fallback.model:
        return fallback
    return HashEmbedder(dimension=choice.dimension, model=choice.name)


__all__ = [
    "DIMENSION",
    "PlatformFixture",
    "RecordingQueue",
    "build_platform",
]
