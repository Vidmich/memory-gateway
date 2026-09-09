"""Moving one organization's vectors from one backend to another, without a gap.

Task 17's reindex already answers "rebuild a collection beside the live one and make the
new one live". A backend migration is the same procedure aimed at a different axis, and it
reuses the shape rather than the code: build in the target, verify, promote, drop the
source after a grace period.

**The one difference that changes the design: this copies vectors instead of re-embedding
them.** Same model, same width — so re-embedding would be an expense with no effect. Two
consequences follow, and both are worth stating because they are what makes a migration
*cheaper* than a reindex rather than merely similar:

* :meth:`~app.services.vector_index.VectorIndexAdmin.scroll` is asked for vectors, which a
  reindex deliberately never does.
* There is **no cursor, and no run table.** A reindex persists its cursor because a point
  costs an embedding call, so redoing a page costs money. Here a point costs a network
  write, so an interrupted copy simply starts again — and starting again is *correct*, not
  merely tolerable, because every point id is a deterministic UUIDv5 and every write is an
  upsert. The binding row is the whole state machine, which is one fewer table and one
  fewer thing that can disagree with itself.

**Memory facts are rebuilt, not copied, and that asymmetry is deliberate.** A document
chunk exists only in the vector store: its text is a payload there, and the original file
would have to be re-extracted to recover it. A memory fact is the opposite —
``memory_facts`` is the record and the vector is an index over it, which is the entire
reason recall reads ids from the index and rows from PostgreSQL. So the cheapest *correct*
move differs per kind, and rebuilding facts from their rows also repairs anything the
source index had drifted on. It costs embedding calls for a fact count that is small beside
a document corpus.

**Reads never move until the promotion.** A copy in flight is invisible: the binding's
``backend`` is what a search resolves through, and it is written once, at the end. A
migration that stalls, fails or is abandoned therefore costs disk and nothing else — which
is what makes it safe to start one on a Friday.

**The source is dropped later, on purpose.** Replicas cache the binding
(:data:`~app.services.vector_backends.BINDING_CACHE_SECONDS`), so for a few seconds after
the promotion some of them still read the source. Dropping it immediately would turn a
stale read into an empty one — a retrieval outage for a slice of traffic that, under
``fail_open``, nobody would see. :func:`~app.services.vector_backends.check_grace_period`
refuses a grace period short enough for that to happen.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace

from app.core.errors import Conflict, Validation
from app.core.tenancy import TenantScope
from app.services.embeddings import Embedder
from app.services.end_user_store import EndUserStore
from app.services.fact_vectors import FactPoint, fact_payload
from app.services.jobs import DROP_MIGRATION_SOURCE, MIGRATE_VECTORS, JobQueue, JobRequest
from app.services.vector_backends import VectorBackends, check_grace_period
from app.services.vector_binding_store import Binding
from app.services.vector_index import COPY_BATCH, VectorIndexAdmin, successor

logger = logging.getLogger(__name__)

#: How long the source stays after the promotion. Fifteen minutes: long enough that every
#: replica has re-read the binding many times over, short enough that an operator watching
#: the migration sees it finish. Validated against the cache TTL at construction.
DROP_GRACE_SECONDS = 900.0

#: Points per round trip, matching the reindexer's.
MIGRATE_BATCH = COPY_BATCH

#: Pause between pages, so a migration cannot crowd out serving on either backend.
PAGE_PAUSE_SECONDS = 0.05

#: End users read per page when facts are rebuilt.
END_USER_PAGE = 100

#: Exact, like the reindexer's: an off-by-one here is a chunk that will never be retrieved
#: again, and there is no reason to accept one.
COUNT_TOLERANCE = 0


class MigrationInFlight(Conflict):
    def __init__(self, organization_id: uuid.UUID, target: str) -> None:
        super().__init__(
            f"organization {organization_id} is already migrating to {target}",
        )


class AlreadyThere(Validation):
    def __init__(self, backend: str) -> None:
        super().__init__(f"this organization's vectors are already on {backend!r}", param="backend")


class NoMigrationInFlight(Conflict):
    def __init__(self, organization_id: uuid.UUID) -> None:
        super().__init__(f"organization {organization_id} has no migration in flight")


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What a migration would move, shown before it is started.

    ``points`` and ``facts`` rather than a cost estimate, which is the difference from a
    reindex worth putting on the screen: nothing here is re-embedded except the facts, so
    the number an operator needs is "how long" rather than "how much".
    """

    organization_id: uuid.UUID
    source: str
    target: str
    source_collection: str | None
    target_collection: str
    points: int
    facts: int


@dataclass(frozen=True, slots=True)
class MigrationResult:
    organization_id: uuid.UUID
    source: str
    target: str
    collection: str
    points: int
    facts: int


class VectorMigrator:
    def __init__(
        self,
        backends: VectorBackends,
        *,
        end_users: EndUserStore,
        embedder: Embedder,
        queue: JobQueue | None = None,
        batch_size: int = MIGRATE_BATCH,
        pause_seconds: float = PAGE_PAUSE_SECONDS,
        grace_seconds: float = DROP_GRACE_SECONDS,
    ) -> None:
        check_grace_period(grace_seconds)
        self._backends = backends
        self._end_users = end_users
        self._embedder = embedder
        self._queue = queue
        self._batch = batch_size
        self._pause = pause_seconds
        self._grace = grace_seconds

    # -- planning ---------------------------------------------------------

    async def plan(self, organization_id: uuid.UUID, *, target: str) -> MigrationPlan:
        binding = await self._backends.binding_for(organization_id)
        if binding.migrating:
            raise MigrationInFlight(organization_id, binding.target or "")
        if target == binding.backend:
            raise AlreadyThere(target)
        # Raises `UnknownBackend` naming the enabled set, which is the useful answer for an
        # operator picking from a list.
        self._backends.require(target)

        source = self._backends.require(binding.backend)
        live = await source.admin.live_collection(organization_id)
        return MigrationPlan(
            organization_id=organization_id,
            source=binding.backend,
            target=target,
            source_collection=live,
            target_collection=successor(organization_id, live),
            points=await source.admin.count_points(live) if live else 0,
            facts=await source.facts.count(organization_id),
        )

    # -- starting ---------------------------------------------------------

    async def start(self, organization_id: uuid.UUID, *, target: str) -> MigrationPlan:
        """Record the intent and, if there is a queue, enqueue the work.

        The row is written *before* anything is copied, which is what makes a second
        request a conflict rather than a second copy running beside the first.
        """
        plan = await self.plan(organization_id, target=target)
        binding = await self._backends.binding_for(organization_id)
        await self._backends.bindings.put(
            replace(
                binding,
                status="migrating",
                target=plan.target,
                target_collection=plan.target_collection,
            )
        )
        logger.info(
            "vector migration started",
            extra={
                "organization_id": str(organization_id),
                "from": plan.source,
                "to": plan.target,
                "points": plan.points,
                "facts": plan.facts,
            },
        )
        if self._queue is not None:
            await self._queue.enqueue(
                JobRequest(
                    name=MIGRATE_VECTORS,
                    payload={"organization_id": str(organization_id)},
                    # Keyed by the organization and the destination, so a retry of the same
                    # migration deduplicates and a genuinely new one does not.
                    idempotency_key=f"migrate:{organization_id}:{plan.target}",
                )
            )
        return plan

    # -- running ----------------------------------------------------------

    async def run(self, organization_id: uuid.UUID) -> MigrationResult:
        """Copy, verify, promote. Safe to call twice: every write is an upsert."""
        binding = await self._backends.binding_for(organization_id)
        if not binding.migrating or binding.target is None or binding.target_collection is None:
            raise NoMigrationInFlight(organization_id)

        source = self._backends.require(binding.backend)
        target = self._backends.require(binding.target)
        collection = binding.target_collection

        live = await source.admin.live_collection(organization_id)
        points = 0
        if live is not None:
            width = await source.admin.collection_dimension(live)
            await target.admin.create_collection(
                collection, dimension=width or self._embedder.dimension
            )
            points = await self._copy(
                source_admin=source.admin,
                target_admin=target.admin,
                source=live,
                target=collection,
            )
            await self._verify(
                source_admin=source.admin,
                target_admin=target.admin,
                source=live,
                target=collection,
            )

        facts = await self._rebuild_facts(organization_id, target_kind=binding.target)

        # The backend-specific half of the promotion: an alias swap for Qdrant, a pointer
        # write for Chroma. Then the binding, which is the promotion everything else sees.
        await target.admin.promote(organization_id, collection)
        await self._backends.bindings.put(
            Binding(
                organization_id=organization_id,
                backend=binding.target,
                # Recorded only where the backend cannot answer for itself. Carrying a
                # stale Chroma pointer into a Qdrant binding would be a second record of
                # the same fact, disagreeing with the alias.
                collection=collection if target.pointer_backed else None,
                status="bound",
            )
        )
        logger.info(
            "vector migration promoted",
            extra={
                "organization_id": str(organization_id),
                "from": binding.backend,
                "to": binding.target,
                "collection": collection,
                "points": points,
                "facts": facts,
            },
        )
        await self._schedule_drop(organization_id, backend=binding.backend, collection=live)
        return MigrationResult(
            organization_id=organization_id,
            source=binding.backend,
            target=binding.target,
            collection=collection,
            points=points,
            facts=facts,
        )

    async def _copy(
        self,
        *,
        source_admin: VectorIndexAdmin,
        target_admin: VectorIndexAdmin,
        source: str,
        target: str,
    ) -> int:
        copied = 0
        cursor: str | None = None
        while True:
            page = await source_admin.scroll(
                source, cursor=cursor, limit=self._batch, with_vectors=True
            )
            carrying = [point for point in page.points if point.vector]
            if len(carrying) != len(page.points):
                # A backend that answered a with-vectors scroll without vectors would
                # otherwise write empty vectors and pass every count check — producing an
                # index that exists, answers nothing, and looks migrated.
                raise RuntimeError(
                    f"{source} returned {len(page.points) - len(carrying)} points with no "
                    "vector; a migration cannot copy what it cannot read"
                )
            if carrying:
                await target_admin.upsert_into(target, carrying)
                copied += len(carrying)
            cursor = page.cursor
            if cursor is None:
                return copied
            if self._pause:
                await asyncio.sleep(self._pause)

    async def _verify(
        self,
        *,
        source_admin: VectorIndexAdmin,
        target_admin: VectorIndexAdmin,
        source: str,
        target: str,
    ) -> None:
        """Counts, then a search. Raising here leaves the source live and loses nothing.

        The same two checks the reindexer runs, and for the same reason the second one
        exists: a collection holding the right number of points that answers no query at
        all is one nothing can retrieve from, and the place to find that out is here rather
        than in a support ticket.
        """
        expected = await source_admin.count_points(source)
        actual = await target_admin.count_points(target)
        if actual < expected - COUNT_TOLERANCE:
            raise RuntimeError(f"{target} holds {actual} points and {source} holds {expected}")
        if not expected:
            return
        page = await target_admin.scroll(target, cursor=None, limit=1, with_vectors=True)
        if not page.points or not page.points[0].vector:
            raise RuntimeError(f"{target} reports {actual} points but returned none to read")
        found = await target_admin.search_in(target, page.points[0].vector, limit=1)
        if not found:
            raise RuntimeError(f"a sample search against {target} returned nothing")

    async def _rebuild_facts(self, organization_id: uuid.UUID, *, target_kind: str) -> int:
        """Re-embed this organization's memory facts into the target backend.

        From ``memory_facts`` rather than from the source index, because the row is the
        record — see the module docstring. Superseded and expired rows are carried over
        too: recall filters them by reading the rows, and an index missing them would make
        "why did it say that last month" unanswerable after a migration.
        """
        target = self._backends.require(target_kind)
        await target.facts.ensure_collection(organization_id, dimension=self._embedder.dimension)

        written = 0
        scope = TenantScope.of_organization(organization_id)
        after: uuid.UUID | None = None
        while True:
            async with self._end_users.begin(scope) as transaction:
                people = await transaction.end_users(after=after, limit=END_USER_PAGE)
                batch: list[FactPoint] = []
                for person in people:
                    facts = await transaction.all_facts(person.id)
                    if not facts:
                        continue
                    vectors = await self._embedder.embed([fact.text for fact in facts])
                    batch.extend(
                        FactPoint(
                            id=str(fact.id),
                            vector=vector,
                            payload=fact_payload(
                                organization_id=organization_id,
                                end_user_id=person.id,
                                kind=fact.kind,
                                confidence=fact.confidence,
                                created_at=fact.created_at.timestamp(),
                            ),
                        )
                        for fact, vector in zip(facts, vectors, strict=True)
                    )
            if batch:
                await target.facts.upsert(organization_id, batch)
                written += len(batch)
            if len(people) < END_USER_PAGE:
                return written
            after = people[-1].id

    # -- finishing --------------------------------------------------------

    async def _schedule_drop(
        self, organization_id: uuid.UUID, *, backend: str, collection: str | None
    ) -> None:
        if self._queue is None or collection is None:
            return
        await self._queue.enqueue(
            JobRequest(
                name=DROP_MIGRATION_SOURCE,
                payload={
                    "organization_id": str(organization_id),
                    "backend": backend,
                    "collection": collection,
                },
                idempotency_key=f"drop-source:{organization_id}:{backend}:{collection}",
                delay_seconds=self._grace,
            )
        )

    async def drop_source(
        self, organization_id: uuid.UUID, *, backend: str, collection: str
    ) -> None:
        """Remove what the migration left behind, after the grace period.

        Refuses if the organization has since been bound back to that backend — a
        migration reversed inside the grace window would otherwise have its *live* index
        dropped by a job scheduled before the reversal.
        """
        binding = await self._backends.binding_for(organization_id)
        if binding.backend == backend:
            logger.info(
                "skipping a migration source drop; the organization is bound here again",
                extra={"organization_id": str(organization_id), "backend": backend},
            )
            return
        held = self._backends.require(backend)
        await held.admin.drop_collection(collection)
        await held.facts.drop(organization_id)
        logger.info(
            "dropped a migrated-from collection",
            extra={
                "organization_id": str(organization_id),
                "backend": backend,
                "collection": collection,
            },
        )

    async def cancel(self, organization_id: uuid.UUID) -> None:
        """Abandon a migration in flight and remove what it has built so far.

        Nothing about reads changes, because nothing about reads ever changed: the binding
        still names the source and always did.
        """
        binding = await self._backends.binding_for(organization_id)
        if not binding.migrating or binding.target is None:
            raise NoMigrationInFlight(organization_id)
        target = self._backends.require(binding.target)
        if binding.target_collection is not None:
            await target.admin.drop_collection(binding.target_collection)
        await target.facts.drop(organization_id)
        await self._backends.bindings.put(
            replace(binding, status="bound", target=None, target_collection=None)
        )
        logger.info(
            "vector migration cancelled",
            extra={"organization_id": str(organization_id), "target": binding.target},
        )


__all__ = [
    "COUNT_TOLERANCE",
    "DROP_GRACE_SECONDS",
    "AlreadyThere",
    "MigrationInFlight",
    "MigrationPlan",
    "MigrationResult",
    "NoMigrationInFlight",
    "VectorMigrator",
]
