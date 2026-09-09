"""Two organizations, two backends, one process.

This is the claim task 19 exists to make, and the checks below are the ones that would
fail if any of it were wired by accident: that a search reaches the backend the binding
names and no other, that an unbound organization is *placed* rather than resolved afresh
every time, that a backend nobody configured cannot be named, and that one backend being
down is one backend's problem.

The stores here are the in-memory ones rather than doubles. That matters for the isolation
checks: asserting "the other organization's data is untouched" against a mock only asserts
that a mock was not called.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from app.core.config import get_settings
from app.core.errors import Validation
from app.services.fact_vectors import FactPoint, FactVectorStore, MemoryFactVectorStore
from app.services.vector_backends import (
    BINDING_CACHE_SECONDS,
    Backend,
    BindingLiveCollections,
    CachedBindings,
    RoutingFactVectorStore,
    RoutingVectorStore,
    UnknownBackend,
    VectorBackends,
    check_grace_period,
    enabled_kinds,
    single_backend,
)
from app.services.vector_binding_store import Binding, MemoryVectorBindingStore
from app.services.vector_index import MemoryVectorIndexAdmin
from app.services.vector_store import (
    ChunkPoint,
    Match,
    MemoryVectorStore,
    VectorStore,
    point_id,
)

DIMENSION = 8


def axis(index: int) -> list[float]:
    return [1.0 if position == index else 0.0 for position in range(DIMENSION)]


def a_backend(kind: str) -> Backend:
    vectors = MemoryVectorStore()
    return Backend(
        kind=kind,
        store=vectors,
        facts=MemoryFactVectorStore(),
        admin=MemoryVectorIndexAdmin(vectors),
    )


def two_backends(
    *, default: str = "qdrant", bindings: MemoryVectorBindingStore | None = None
) -> tuple[VectorBackends, dict[str, Backend]]:
    built = {"qdrant": a_backend("qdrant"), "chroma": a_backend("chroma")}
    registry = VectorBackends(
        built,
        bindings if bindings is not None else MemoryVectorBindingStore(),
        default=default,
    )
    return registry, built


async def store_a_chunk(store: VectorStore, organization: uuid.UUID, text: str) -> None:
    document = uuid.uuid4()
    await store.ensure_collection(organization, dimension=DIMENSION)
    await store.upsert(
        organization,
        [
            ChunkPoint(
                id=point_id(document, 0),
                vector=axis(0),
                payload={
                    "document_id": str(document),
                    "connector_id": str(uuid.uuid4()),
                    "text": text,
                },
            )
        ],
    )


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


async def test_two_organizations_on_two_backends_never_see_each_other() -> None:
    """The demo, as an assertion. Acme on Qdrant and Globex on Chroma, in one process,
    each retrieving its own documents and nothing else."""
    bindings = MemoryVectorBindingStore()
    registry, built = two_backends(bindings=bindings)
    acme, globex = uuid.uuid4(), uuid.uuid4()
    await bindings.put(Binding(acme, backend="qdrant"))
    await bindings.put(Binding(globex, backend="chroma"))
    routing = RoutingVectorStore(registry)

    await store_a_chunk(routing, acme, "acme handbook")
    await store_a_chunk(routing, globex, "globex handbook")

    assert [match.text for match in await routing.search(acme, axis(0))] == ["acme handbook"]
    assert [match.text for match in await routing.search(globex, axis(0))] == ["globex handbook"]
    # And the data really is in different places, not merely filtered.
    assert await built["qdrant"].store.count(globex) == 0
    assert await built["chroma"].store.count(acme) == 0


async def test_conversation_memory_routes_the_same_way() -> None:
    """Both kinds move together, or an organization ends up with its documents in one
    backend and its memory in another — a state no screen can explain."""
    bindings = MemoryVectorBindingStore()
    registry, built = two_backends(bindings=bindings)
    organization, end_user = uuid.uuid4(), uuid.uuid4()
    await bindings.put(Binding(organization, backend="chroma"))
    facts: FactVectorStore = RoutingFactVectorStore(registry)

    await facts.ensure_collection(organization, dimension=DIMENSION)
    await facts.upsert(
        organization,
        [
            FactPoint(
                id=str(uuid.uuid4()),
                vector=axis(0),
                payload={"end_user_id": str(end_user), "org_id": str(organization)},
            )
        ],
    )

    assert await built["chroma"].facts.count(organization) == 1
    assert await built["qdrant"].facts.count(organization) == 0


async def test_an_unbound_organization_is_placed_and_the_row_is_written() -> None:
    """A resolution that did not record itself would mean an operator changing the default
    silently moves every tenant that has not been explicitly placed — including ones with
    a fully populated index. The default decides where a tenant *starts*."""
    bindings = MemoryVectorBindingStore()
    registry, _ = two_backends(default="chroma", bindings=bindings)
    organization = uuid.uuid4()

    assert await registry.kind_for(organization) == "chroma"

    stored = await bindings.get(organization)
    assert stored is not None
    assert stored.backend == "chroma"


async def test_changing_the_default_does_not_move_an_organization_already_placed() -> None:
    bindings = MemoryVectorBindingStore()
    first, _ = two_backends(default="qdrant", bindings=bindings)
    organization = uuid.uuid4()
    await first.kind_for(organization)

    second, _ = two_backends(default="chroma", bindings=bindings)

    assert await second.kind_for(organization) == "qdrant"


async def test_dropping_an_organization_reaches_every_backend() -> None:
    """Offboarding, and the reason it is not routed: a tenant whose migration was
    abandoned has data in two backends, and an erasure report covering only the one being
    read from is worthless for the purpose it exists for."""
    bindings = MemoryVectorBindingStore()
    registry, built = two_backends(bindings=bindings)
    organization = uuid.uuid4()
    await bindings.put(Binding(organization, backend="qdrant"))
    routing = RoutingVectorStore(registry)
    await store_a_chunk(routing, organization, "live")
    # A half-finished migration: the copy exists in the other backend.
    await store_a_chunk(built["chroma"].store, organization, "copy")

    await routing.drop(organization)

    assert await built["qdrant"].store.count(organization) == 0
    assert await built["chroma"].store.count(organization) == 0


# ---------------------------------------------------------------------------
# what may be named
# ---------------------------------------------------------------------------


async def test_a_backend_this_deployment_does_not_have_cannot_be_named() -> None:
    """And the refusal names what *is* available, because the caller is an operator
    choosing from a list rather than a program that guessed."""
    registry = single_backend(
        store=MemoryVectorStore(),
        facts=MemoryFactVectorStore(),
        admin=MemoryVectorIndexAdmin(MemoryVectorStore()),
    )

    with pytest.raises(UnknownBackend) as caught:
        registry.require("chroma")

    assert "chroma" in str(caught.value)
    assert "qdrant" in str(caught.value)
    # A Validation, so the API answers 422 with the parameter named rather than a 500.
    assert isinstance(caught.value, Validation)
    assert caught.value.param == "backend"


async def test_a_registry_cannot_default_to_a_backend_it_does_not_hold() -> None:
    with pytest.raises(UnknownBackend):
        VectorBackends(
            {"qdrant": a_backend("qdrant")}, MemoryVectorBindingStore(), default="chroma"
        )


def test_chroma_is_enabled_only_when_it_is_configured() -> None:
    """The enabled set comes from the environment and from nowhere else — the whole
    reason a tenant can name a backend at all is that naming one cannot reach a URL."""
    settings = get_settings()

    assert enabled_kinds(settings.model_copy(update={"chroma_url": None})) == ["qdrant"]
    assert enabled_kinds(settings.model_copy(update={"chroma_url": "http://chroma:8000"})) == [
        "qdrant",
        "chroma",
    ]


def test_the_default_backend_must_be_reachable() -> None:
    """A default naming a backend this deployment cannot reach is a deployment where every
    newly created organization silently has no index."""
    settings = get_settings()

    with pytest.raises(ValueError, match="CHROMA_URL"):
        settings.model_copy(update={"chroma_url": None}).model_validate(
            settings.model_dump() | {"chroma_url": None, "default_vector_backend": "chroma"}
        )


# ---------------------------------------------------------------------------
# isolation and caching
# ---------------------------------------------------------------------------


async def test_one_backend_failing_is_one_backend_s_problem() -> None:
    """The claim the readiness rule depends on. An organization on a healthy backend must
    not be affected by an outage somewhere else in the registry."""

    class Refusing(MemoryVectorStore):
        async def search(
            self,
            organization_id: uuid.UUID,
            vector: Sequence[float],
            *,
            connector_ids: Sequence[uuid.UUID] = (),
            limit: int = 10,
            min_score: float = 0.0,
        ) -> list[Match]:
            raise ConnectionRefusedError("connection refused")

    bindings = MemoryVectorBindingStore()
    broken = Backend(
        kind="chroma",
        store=Refusing(),
        facts=MemoryFactVectorStore(),
        admin=MemoryVectorIndexAdmin(MemoryVectorStore()),
    )
    registry = VectorBackends(
        {"qdrant": a_backend("qdrant"), "chroma": broken}, bindings, default="qdrant"
    )
    healthy, affected = uuid.uuid4(), uuid.uuid4()
    await bindings.put(Binding(healthy, backend="qdrant"))
    await bindings.put(Binding(affected, backend="chroma"))
    routing = RoutingVectorStore(registry)
    await store_a_chunk(routing, healthy, "still here")

    with pytest.raises(ConnectionRefusedError):
        await routing.search(affected, axis(0))

    assert [match.text for match in await routing.search(healthy, axis(0))] == ["still here"]


async def test_a_cached_binding_is_not_read_twice() -> None:
    """The binding is on the retrieval path. Without the cache every search would be a
    database round trip ahead of the vector query."""
    counting = _CountingBindings()
    cached = CachedBindings(counting)
    organization = uuid.uuid4()
    await cached.put(Binding(organization, backend="qdrant"))

    await cached.get(organization)
    await cached.get(organization)

    assert counting.reads == 1


async def test_a_write_invalidates_immediately_in_this_process() -> None:
    """The operator who just pressed the button must not see the old value. Other replicas
    catch up within the TTL, which is what `check_grace_period` bounds."""
    counting = _CountingBindings()
    cached = CachedBindings(counting)
    organization = uuid.uuid4()
    await cached.put(Binding(organization, backend="qdrant"))
    await cached.get(organization)

    await cached.put(Binding(organization, backend="chroma"))
    found = await cached.get(organization)

    assert found is not None
    assert found.backend == "chroma"


def test_a_grace_period_inside_the_cache_window_is_refused() -> None:
    """A replica holding a cached binding still reads the source for up to the TTL after a
    promotion. Dropping the source inside that window turns a stale read into an empty
    one — a retrieval outage for a slice of traffic, invisible under ``fail_open``."""
    with pytest.raises(ValueError, match="binding cache"):
        check_grace_period(BINDING_CACHE_SECONDS)

    check_grace_period(BINDING_CACHE_SECONDS + 1)


async def test_the_chroma_pointer_never_disturbs_the_binding_it_shares_a_row_with() -> None:
    """``collection`` and ``backend`` live on one row and are written by different things.
    A pointer write that replaced the row would silently move an organization back to the
    default backend on its first ingestion."""
    bindings = MemoryVectorBindingStore()
    organization = uuid.uuid4()
    await bindings.put(Binding(organization, backend="chroma"))
    live = BindingLiveCollections(bindings)

    await live.point(organization, f"org_{organization}_docs_v1")

    stored = await bindings.get(organization)
    assert stored is not None
    assert stored.backend == "chroma"
    assert stored.collection == f"org_{organization}_docs_v1"


class _CountingBindings(MemoryVectorBindingStore):
    reads: int = 0

    async def get(self, organization_id: uuid.UUID) -> Binding | None:
        self.reads += 1
        return await super().get(organization_id)
