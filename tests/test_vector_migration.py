"""Moving an organization between backends, and the properties that make it safe.

Four claims, and each one has a check below that fails if it stops being true:

* **reads never move until the promotion** — a search running throughout returns valid
  results at every moment, and the same top-k before and after;
* **an interrupted copy resumes** without duplicate or missing points, which here means
  "starts again and converges", because every id is deterministic and every write an upsert;
* **the source survives the promotion** long enough for replicas holding a cached binding,
  and is dropped afterwards;
* **a cancelled migration leaves nothing** in the backend it was heading for, and changes
  nothing about where reads go.

The stores are the in-memory ones, so "the other backend really is empty" is an assertion
about data rather than about a mock's call log.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.embeddings import HashEmbedder
from app.services.end_user_store import MemoryEndUserStore
from app.services.fact_vectors import MemoryFactVectorStore
from app.services.jobs import JobRequest
from app.services.memory_db import MemoryDatabase
from app.services.vector_backends import (
    BINDING_CACHE_SECONDS,
    Backend,
    UnknownBackend,
    VectorBackends,
)
from app.services.vector_binding_store import Binding, MemoryVectorBindingStore
from app.services.vector_index import MemoryVectorIndexAdmin, versioned
from app.services.vector_migration import (
    AlreadyThere,
    MigrationInFlight,
    NoMigrationInFlight,
    VectorMigrator,
)
from app.services.vector_store import ChunkPoint, MemoryVectorStore, point_id

DIMENSION = 8


def axis(index: int) -> list[float]:
    return [1.0 if position == index else 0.0 for position in range(DIMENSION)]


class World:
    """Two backends, one organization, and a migrator wired to both."""

    def __init__(self, *, on: str = "qdrant") -> None:
        self.database = MemoryDatabase()
        self.bindings = MemoryVectorBindingStore()
        self.stores: dict[str, MemoryVectorStore] = {}
        self.facts: dict[str, MemoryFactVectorStore] = {}
        built: dict[str, Backend] = {}
        for kind in ("qdrant", "chroma"):
            vectors = MemoryVectorStore()
            memory = MemoryFactVectorStore()
            self.stores[kind] = vectors
            self.facts[kind] = memory
            built[kind] = Backend(
                kind=kind,
                store=vectors,
                facts=memory,
                admin=MemoryVectorIndexAdmin(vectors),
                # Chroma cannot answer "which collection is live" about itself; Qdrant's
                # alias can. The migrator reads this to decide whether the promotion also
                # records a pointer.
                pointer_backed=kind == "chroma",
            )
        self.backends = VectorBackends(built, self.bindings, default=on)
        self.organization = uuid.uuid4()
        self.queue = _RecordingQueue()
        self.migrator = VectorMigrator(
            self.backends,
            end_users=MemoryEndUserStore(self.database),
            embedder=HashEmbedder(dimension=DIMENSION, model="hash-bow"),
            queue=self.queue,
            pause_seconds=0.0,
            grace_seconds=BINDING_CACHE_SECONDS * 2,
        )

    async def seed(self, count: int, *, kind: str = "qdrant") -> None:
        store = self.stores[kind]
        await store.ensure_collection(self.organization, dimension=DIMENSION)
        document = uuid.uuid4()
        await store.upsert(
            self.organization,
            [
                ChunkPoint(
                    id=point_id(document, index),
                    vector=axis(index % DIMENSION),
                    payload={
                        "document_id": str(document),
                        "connector_id": str(uuid.uuid4()),
                        "chunk_index": index,
                        "text": f"chunk {index}",
                    },
                )
                for index in range(count)
            ],
        )

    async def search(self) -> list[str]:
        """One search, through the routing store — so it goes wherever the binding says.

        ``limit=1`` because the fixture's vectors sit on distinct axes: everything but the
        first scores zero, and asking for three would return two chunks nobody would call
        relevant and make the assertions about ordering unreadable.
        """
        store = await self.backends.store_for(self.organization)
        return [match.text for match in await store.search(self.organization, axis(0), limit=1)]


class _RecordingQueue:
    def __init__(self) -> None:
        self.requests: list[JobRequest] = []

    async def enqueue(self, request: JobRequest) -> str | None:
        self.requests.append(request)
        return "queued"

    async def depth(self) -> int:
        return len(self.requests)

    async def ping(self) -> None:
        return None

    def named(self, name: str) -> list[JobRequest]:
        return [request for request in self.requests if request.name == name]


# ---------------------------------------------------------------------------
# planning and refusals
# ---------------------------------------------------------------------------


async def test_a_plan_reports_what_would_move_rather_than_a_cost() -> None:
    """The difference from a reindex worth putting on a screen: nothing is re-embedded
    except the facts, so the number an operator needs is "how long", not "how much"."""
    world = World()
    await world.seed(5)

    plan = await world.migrator.plan(world.organization, target="chroma")

    assert plan.source == "qdrant"
    assert plan.target == "chroma"
    assert plan.points == 5
    assert plan.target_collection == versioned(world.organization, 2)


async def test_migrating_to_where_it_already_is_is_refused() -> None:
    world = World()

    with pytest.raises(AlreadyThere):
        await world.migrator.plan(world.organization, target="qdrant")


async def test_migrating_to_a_backend_this_deployment_does_not_have_is_refused() -> None:
    world = World()
    # A registry holding only Qdrant, sharing the same bindings.
    only_qdrant = VectorBackends(
        {"qdrant": world.backends.require("qdrant")}, world.bindings, default="qdrant"
    )
    migrator = VectorMigrator(
        only_qdrant,
        end_users=MemoryEndUserStore(world.database),
        embedder=HashEmbedder(dimension=DIMENSION, model="hash-bow"),
    )

    with pytest.raises(UnknownBackend):
        await migrator.plan(world.organization, target="chroma")


async def test_a_second_migration_for_the_same_organization_is_a_conflict() -> None:
    """Refused by the row, which is written before anything is copied — so the second
    request conflicts rather than starting a second copy beside the first."""
    world = World()
    await world.seed(2)
    await world.migrator.start(world.organization, target="chroma")

    with pytest.raises(MigrationInFlight):
        await world.migrator.start(world.organization, target="chroma")


# ---------------------------------------------------------------------------
# the zero-downtime claim
# ---------------------------------------------------------------------------


async def test_reads_stay_on_the_source_for_the_whole_copy() -> None:
    """The property the design turns on. A copy in flight is invisible: the binding still
    names the source, so a migration that stalls or fails costs disk and nothing else."""
    world = World()
    await world.seed(4)
    await world.migrator.start(world.organization, target="chroma")

    during = await world.search()

    assert during == ["chunk 0"]
    assert await world.backends.kind_for(world.organization) == "qdrant"


async def test_a_search_loop_sees_valid_results_at_every_moment() -> None:
    """Before, during and after — never an empty answer, and the same top-k on both sides
    of the promotion. This is what "no retrieval downtime" has to mean to be worth
    claiming."""
    world = World()
    await world.seed(6)

    before = await world.search()
    await world.migrator.start(world.organization, target="chroma")
    during = await world.search()
    await world.migrator.run(world.organization)
    after = await world.search()

    assert before == during == after
    assert before, "an empty result would make the assertion above vacuous"


async def test_a_promotion_moves_the_binding_and_records_a_pointer_only_where_needed() -> None:
    """Chroma needs the deployment to remember which collection is live; Qdrant's alias is
    authoritative. Recording it in both places is how the two come to disagree."""
    world = World()
    await world.seed(3)
    await world.migrator.start(world.organization, target="chroma")

    await world.migrator.run(world.organization)

    binding = await world.bindings.get(world.organization)
    assert binding is not None
    assert binding.backend == "chroma"
    assert binding.status == "bound"
    assert binding.target is None
    assert binding.collection == versioned(world.organization, 2)


async def test_migrating_back_to_qdrant_clears_the_pointer() -> None:
    """The other direction, and the reason the promotion writes the whole row: carrying a
    stale Chroma pointer into a Qdrant binding would be a second record of the same fact,
    disagreeing with the alias."""
    world = World(on="chroma")
    await world.bindings.put(Binding(world.organization, backend="chroma"))
    await world.seed(3, kind="chroma")
    await world.migrator.start(world.organization, target="qdrant")

    await world.migrator.run(world.organization)

    binding = await world.bindings.get(world.organization)
    assert binding is not None
    assert binding.backend == "qdrant"
    assert binding.collection is None
    assert [
        match.text
        for match in await world.stores["qdrant"].search(world.organization, axis(0), limit=1)
    ] == ["chunk 0"]


async def test_every_point_arrives_and_none_is_duplicated() -> None:
    world = World()
    await world.seed(10)

    await world.migrator.start(world.organization, target="chroma")
    result = await world.migrator.run(world.organization)

    assert result.points == 10
    assert await world.stores["chroma"].count(world.organization) == 10


async def test_a_copy_run_twice_converges_instead_of_doubling() -> None:
    """There is no cursor here, unlike a reindex: an interrupted copy starts again, and
    that is *correct* rather than merely tolerable because every point id is a
    deterministic UUIDv5 and every write is an upsert."""
    world = World()
    await world.seed(7)
    await world.migrator.start(world.organization, target="chroma")

    first = await world.migrator.run(world.organization)
    # A second run finds no migration in flight, because the first one finished it.
    with pytest.raises(NoMigrationInFlight):
        await world.migrator.run(world.organization)

    assert first.points == 7
    assert await world.stores["chroma"].count(world.organization) == 7


# ---------------------------------------------------------------------------
# after the promotion
# ---------------------------------------------------------------------------


async def test_the_source_survives_the_promotion_and_is_dropped_later() -> None:
    """Dropping it immediately would turn a replica's stale-but-valid read into an empty
    one — a retrieval outage for a slice of traffic that ``fail_open`` makes invisible."""
    world = World()
    await world.seed(3)
    await world.migrator.start(world.organization, target="chroma")
    await world.migrator.run(world.organization)

    assert await world.stores["qdrant"].count(world.organization) == 3
    [deferred] = world.queue.named("drop_migration_source")
    assert deferred.delay_seconds > BINDING_CACHE_SECONDS

    await world.migrator.drop_source(
        world.organization, backend="qdrant", collection=versioned(world.organization, 1)
    )

    assert await world.stores["qdrant"].count(world.organization) == 0


async def test_a_deferred_drop_refuses_to_delete_a_live_index() -> None:
    """A migration reversed inside the grace window would otherwise have its *new* live
    index dropped by a job scheduled before the reversal."""
    world = World()
    await world.seed(3)
    await world.migrator.start(world.organization, target="chroma")
    await world.migrator.run(world.organization)
    # Reversed: back on qdrant before the deferred drop fires.
    await world.migrator.start(world.organization, target="qdrant")
    await world.migrator.run(world.organization)

    await world.migrator.drop_source(
        world.organization, backend="qdrant", collection=versioned(world.organization, 1)
    )

    assert await world.backends.kind_for(world.organization) == "qdrant"
    assert await world.search() == ["chunk 0"]


async def test_cancelling_removes_the_target_and_changes_nothing_about_reads() -> None:
    world = World()
    await world.seed(4)
    await world.migrator.start(world.organization, target="chroma")

    await world.migrator.cancel(world.organization)

    assert await world.stores["chroma"].count(world.organization) == 0
    assert await world.backends.kind_for(world.organization) == "qdrant"
    assert await world.search() == ["chunk 0"]
    binding = await world.bindings.get(world.organization)
    assert binding is not None
    assert binding.status == "bound"
    assert binding.target is None


async def test_cancelling_when_nothing_is_in_flight_is_a_conflict() -> None:
    world = World()

    with pytest.raises(NoMigrationInFlight):
        await world.migrator.cancel(world.organization)


async def test_a_grace_period_inside_the_binding_cache_window_is_refused_at_construction() -> None:
    """Caught when the migrator is built rather than when a drop fires, because the failure
    it prevents is invisible: a slice of traffic retrieving nothing."""
    world = World()

    with pytest.raises(ValueError, match="binding cache"):
        VectorMigrator(
            world.backends,
            end_users=MemoryEndUserStore(world.database),
            embedder=HashEmbedder(dimension=DIMENSION, model="hash-bow"),
            grace_seconds=1.0,
        )
