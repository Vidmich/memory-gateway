"""Which backend an organization's vectors are on, and the routing that follows from it.

**The lucky break this task was built on.** Every method of every vector port already takes
``organization_id`` as its first argument — because SPEC §5.3 makes the tenant the unit of
isolation, and the collection name carries it. So per-organization routing does not need a
single call site to change: :class:`RoutingVectorStore` *is* a
:class:`~app.services.vector_store.VectorStore`, resolves the backend per call, and
forwards. Ingestion, retrieval, connectors, end users and erasure keep the port they
already had and never learn there is more than one backend.

That is worth stating because the alternative was seriously considered and is much worse:
threading a backend handle through every constructor would have touched forty call sites
to express something none of them care about.

**Connection details are operator configuration; an organization picks a name.** The
backends this deployment can talk to come from the environment — ``QDRANT_URL``,
``CHROMA_URL`` — and nowhere else. An organization's binding names one of them, validated
against the enabled set. A tenant-supplied vector-store URL would be a request this server
makes on their behalf to an address they chose, which is precisely the surface task 18
closed for upstream models; it does not get to come back in through a different door.

**One backend per kind, deliberately.** The task file called for a named set in platform
settings, each entry with its own connection configuration. That is not what this builds,
and the reason is the sentence above: connection details in a database table are
credentials and addresses an operator edits on a screen, which is a second place for the
same class of mistake. What *is* a platform setting is the default for new organizations —
policy, not topology. Two Qdrants for two tenants is a real requirement the day somebody
has it, and it is a change to this module rather than to anything above it.

**Resolution is cached, and the staleness is bounded and benign.** The binding is read on
the retrieval path, so :class:`CachedBindings` holds it for :data:`BINDING_CACHE_SECONDS`.
A replica that has not yet seen a promotion keeps reading the *previous* backend — which
still holds the data, because a migration drops its source only after a grace period that
is required to exceed this TTL. The degradation is "results from a few seconds ago", never
"no results". :func:`check_grace_period` is where that requirement is enforced rather than
described.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.core.config import Settings
from app.core.errors import Validation
from app.services.fact_vectors import FactPoint, FactVectorStore
from app.services.vector_binding_store import (
    Binding,
    MemoryVectorBindingStore,
    VectorBindingStore,
)
from app.services.vector_index import LiveCollections, Page, VectorIndexAdmin
from app.services.vector_store import ChunkPoint, Match, Stored, VectorStore

logger = logging.getLogger(__name__)

#: The backends this build can talk to, in the order a screen should list them.
BACKEND_KINDS = ("qdrant", "chroma")

#: How long a resolved binding is reused before it is read again. Short enough that an
#: operator watching a second replica sees a migration land without wondering, long enough
#: that it is one query every few seconds per process rather than one per search.
BINDING_CACHE_SECONDS = 15.0


class UnknownBackend(Validation):
    """A backend that is not enabled on this deployment.

    A :class:`~app.core.errors.Validation` rather than a bare error because the caller is
    usually an operator choosing from a list, and the useful response names what *is*
    available instead of saying no.
    """

    def __init__(self, name: str, *, available: Sequence[str]) -> None:
        super().__init__(
            f"{name!r} is not a vector backend this deployment offers "
            f"(enabled: {', '.join(available) or 'none'})",
            param="backend",
        )


@dataclass(frozen=True, slots=True)
class Backend:
    """One configured backend: the three ports, its client, and a reachability probe."""

    kind: str
    store: VectorStore
    facts: FactVectorStore
    admin: VectorIndexAdmin
    client: Any = None
    #: What ``/readyz`` calls. A per-backend callable rather than a port method, because
    #: the cheapest way to ask "are you there" differs — one server answers a collection
    #: listing in constant time, another has a heartbeat and would make a listing grow
    #: with the number of tenants. A probe that gets slower as the platform grows is a
    #: probe that eventually times out for reasons unrelated to health.
    ping: Callable[[], Awaitable[Any]] | None = None
    #: Whether this backend needs the deployment to remember which collection is live.
    #: True for Chroma, false for Qdrant, whose alias is authoritative. It is a property of
    #: the backend rather than a branch at the promotion site, because the *reason* a
    #: promotion writes a pointer for one and not the other is that the two record the same
    #: fact in different places — and recording it in both is how they come to disagree.
    pointer_backed: bool = False


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


@dataclass
class CachedBindings:
    """A :class:`~app.services.vector_binding_store.VectorBindingStore` with a TTL in front.

    A decorator rather than a cache inside the resolver, so the Chroma pointer lookups in
    :class:`BindingLiveCollections` and the backend lookups in :class:`VectorBackends`
    share one entry per organization instead of racing each other to the database. Writes
    go through and drop the entry in this process immediately — the operator who just
    pressed the button must not see the old value — and other replicas pick it up within
    the TTL.
    """

    inner: VectorBindingStore
    ttl: float = BINDING_CACHE_SECONDS
    _held: dict[uuid.UUID, tuple[float, Binding | None]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._held = {}

    def invalidate(self, organization_id: uuid.UUID) -> None:
        self._held.pop(organization_id, None)

    async def get(self, organization_id: uuid.UUID) -> Binding | None:
        entry = self._held.get(organization_id)
        now = time.monotonic()
        if entry is not None and now - entry[0] < self.ttl:
            return entry[1]
        found = await self.inner.get(organization_id)
        self._held[organization_id] = (now, found)
        return found

    async def put(self, binding: Binding) -> Binding:
        stored = await self.inner.put(binding)
        self.invalidate(binding.organization_id)
        return stored

    async def forget(self, organization_id: uuid.UUID) -> None:
        await self.inner.forget(organization_id)
        self.invalidate(organization_id)

    async def all(self) -> list[Binding]:
        return await self.inner.all()

    async def on(self, backend: str) -> list[Binding]:
        return await self.inner.on(backend)


@dataclass
class BindingLiveCollections:
    """The :class:`~app.services.vector_index.LiveCollections` port, over the binding row.

    Only a backend that cannot answer "which collection is live" about itself uses this;
    Qdrant's alias is authoritative and its rows leave ``collection`` null, which is what
    keeps the two from ever disagreeing.
    """

    bindings: VectorBindingStore
    default_backend: str = "qdrant"

    async def resolve(self, organization_id: uuid.UUID) -> str | None:
        found = await self.bindings.get(organization_id)
        return None if found is None else found.collection

    async def point(self, organization_id: uuid.UUID, collection: str) -> None:
        found = await self.bindings.get(organization_id)
        current = found or Binding(organization_id, backend=self.default_backend)
        await self.bindings.put(replace(current, collection=collection))

    async def forget(self, organization_id: uuid.UUID) -> None:
        found = await self.bindings.get(organization_id)
        if found is not None:
            await self.bindings.put(replace(found, collection=None))


class VectorBackends:
    """The registry, and the answer to "where does this organization's data live"."""

    def __init__(
        self,
        backends: Mapping[str, Backend],
        bindings: VectorBindingStore,
        *,
        default: str = "qdrant",
    ) -> None:
        if not backends:
            raise ValueError("at least one vector backend must be configured")
        if default not in backends:
            raise UnknownBackend(default, available=sorted(backends))
        self._backends = dict(backends)
        self._bindings = bindings
        self._default = default

    @property
    def default(self) -> str:
        return self._default

    @property
    def bindings(self) -> VectorBindingStore:
        return self._bindings

    def enabled(self) -> list[str]:
        return [kind for kind in BACKEND_KINDS if kind in self._backends]

    def has(self, kind: str) -> bool:
        return kind in self._backends

    def require(self, kind: str) -> Backend:
        found = self._backends.get(kind)
        if found is None:
            raise UnknownBackend(kind, available=self.enabled())
        return found

    def every(self) -> list[Backend]:
        return [self._backends[kind] for kind in self.enabled()]

    async def binding_for(self, organization_id: uuid.UUID) -> Binding:
        """This organization's binding, creating it from the default if there is none.

        The write happens on a read path, which is unusual enough to justify: it happens
        once in an organization's life, and the alternative — resolving the default every
        time without recording it — would mean an operator changing the platform default
        silently moves every tenant that has not been explicitly placed, including ones
        with a fully populated index. The default decides where a tenant *starts*; after
        that the row decides, and that is what makes the default safe to change.
        """
        found = await self._bindings.get(organization_id)
        if found is not None:
            return found
        created = Binding(organization_id=organization_id, backend=self._default)
        logger.info(
            "binding organization to the default vector backend",
            extra={"organization_id": str(organization_id), "backend": self._default},
        )
        return await self._bindings.put(created)

    async def kind_for(self, organization_id: uuid.UUID) -> str:
        return (await self.binding_for(organization_id)).reading_from()

    async def backend_for(self, organization_id: uuid.UUID) -> Backend:
        return self.require(await self.kind_for(organization_id))

    async def store_for(self, organization_id: uuid.UUID) -> VectorStore:
        return (await self.backend_for(organization_id)).store

    async def facts_for(self, organization_id: uuid.UUID) -> FactVectorStore:
        return (await self.backend_for(organization_id)).facts

    async def admin_for(self, organization_id: uuid.UUID) -> VectorIndexAdmin:
        return (await self.backend_for(organization_id)).admin

    async def aclose(self) -> None:
        for backend in self.every():
            closer = getattr(backend.client, "close", None) or getattr(
                backend.client, "aclose", None
            )
            if closer is None:
                continue
            try:
                result = closer()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # pragma: no cover - shutdown must not raise
                logger.warning(
                    "failed to close vector backend", extra={"backend": backend.kind}, exc_info=True
                )


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


class RoutingVectorStore:
    """A :class:`~app.services.vector_store.VectorStore` that picks the backend per call.

    Every method is one resolution and one forward. Written out rather than generated with
    ``__getattr__`` on purpose: the explicit list is what makes a new port method a
    compile-time decision here instead of a silently unrouted call at runtime.
    """

    def __init__(self, backends: VectorBackends) -> None:
        self._backends = backends

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        store = await self._backends.store_for(organization_id)
        await store.ensure_collection(organization_id, dimension=dimension)

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None:
        store = await self._backends.store_for(organization_id)
        await store.upsert(organization_id, points)

    async def delete_document(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
        store = await self._backends.store_for(organization_id)
        await store.delete_document(organization_id, document_id)

    async def delete_connector(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        store = await self._backends.store_for(organization_id)
        await store.delete_connector(organization_id, connector_id)

    async def delete_points(self, organization_id: uuid.UUID, ids: Sequence[str]) -> None:
        store = await self._backends.store_for(organization_id)
        await store.delete_points(organization_id, ids)

    async def drop(self, organization_id: uuid.UUID) -> None:
        """Offboarding, so it drops from **every** backend rather than the live one.

        A tenant whose migration was abandoned has data in two, and an erasure report that
        covered only the one currently being read from would be exactly the kind of
        document that is worthless for the purpose it exists for.
        """
        for backend in self._backends.every():
            await backend.store.drop(organization_id)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        store = await self._backends.store_for(organization_id)
        return await store.dimension(organization_id)

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[Match]:
        store = await self._backends.store_for(organization_id)
        return await store.search(
            organization_id,
            vector,
            connector_ids=connector_ids,
            limit=limit,
            min_score=min_score,
        )

    async def chunks(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, *, limit: int = 500
    ) -> list[Stored]:
        store = await self._backends.store_for(organization_id)
        return await store.chunks(organization_id, document_id, limit=limit)

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int:
        store = await self._backends.store_for(organization_id)
        return await store.count(
            organization_id, connector_id=connector_id, document_id=document_id
        )


class RoutingFactVectorStore:
    """The same, for the conversation-memory index."""

    def __init__(self, backends: VectorBackends) -> None:
        self._backends = backends

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        facts = await self._backends.facts_for(organization_id)
        await facts.ensure_collection(organization_id, dimension=dimension)

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[FactPoint]) -> None:
        facts = await self._backends.facts_for(organization_id)
        await facts.upsert(organization_id, points)

    async def delete(self, organization_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]) -> None:
        facts = await self._backends.facts_for(organization_id)
        await facts.delete(organization_id, fact_ids)

    async def delete_end_user(self, organization_id: uuid.UUID, end_user_id: uuid.UUID) -> None:
        facts = await self._backends.facts_for(organization_id)
        await facts.delete_end_user(organization_id, end_user_id)

    async def drop(self, organization_id: uuid.UUID) -> None:
        for backend in self._backends.every():
            await backend.facts.drop(organization_id)

    async def ids(self, organization_id: uuid.UUID) -> set[str]:
        facts = await self._backends.facts_for(organization_id)
        return await facts.ids(organization_id)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        facts = await self._backends.facts_for(organization_id)
        return await facts.dimension(organization_id)

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        end_user_id: uuid.UUID,
        limit: int = 16,
        min_score: float = 0.0,
    ) -> list[Match]:
        facts = await self._backends.facts_for(organization_id)
        return await facts.search(
            organization_id, vector, end_user_id=end_user_id, limit=limit, min_score=min_score
        )

    async def count(
        self, organization_id: uuid.UUID, *, end_user_id: uuid.UUID | None = None
    ) -> int:
        facts = await self._backends.facts_for(organization_id)
        return await facts.count(organization_id, end_user_id=end_user_id)


class RoutingVectorIndexAdmin:
    """A :class:`~app.services.vector_index.VectorIndexAdmin` bound to one organization.

    The admin's methods take *collection names*, not tenant ids — that is the whole point
    of the second protocol — so it cannot resolve per call the way the stores above do.
    The resolution therefore happens once, when a caller that already knows the
    organization asks for one; :meth:`VectorBackends.admin_for` is that call.

    This class exists only for the operations that legitimately span collections in one
    backend, and it is a plain hand-off. It is written down rather than returned bare so
    the *reason* an admin is per-organization is somewhere a reader will find it.
    """

    def __init__(self, inner: VectorIndexAdmin) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


def enabled_kinds(settings: Settings) -> list[str]:
    """The backends this deployment is configured to talk to.

    Qdrant is always one of them: ``QDRANT_URL`` is required, ``/readyz`` has probed it
    since task 01, and a deployment with no vector backend at all is one where every
    memory feature is silently off.
    """
    kinds = ["qdrant"]
    if settings.chroma_url:
        kinds.append("chroma")
    return kinds


async def build_backends(
    settings: Settings,
    *,
    qdrant_client: Any,
    bindings: VectorBindingStore | None = None,
) -> VectorBackends:
    """Every configured backend, with its clients, ready to route.

    The Chroma client is built here rather than in :class:`~app.core.clients.Clients`
    because constructing it is asynchronous — and because a deployment that does not
    configure Chroma should not import ``chromadb`` at all, which is what makes the
    dependency genuinely optional rather than nominally so.
    """
    from app.services.vector_qdrant import (
        QdrantFactVectorStore,
        QdrantVectorIndexAdmin,
        QdrantVectorStore,
    )

    store = bindings if bindings is not None else MemoryVectorBindingStore()
    cached = store if isinstance(store, CachedBindings) else CachedBindings(store)

    built: dict[str, Backend] = {
        "qdrant": Backend(
            kind="qdrant",
            store=QdrantVectorStore(qdrant_client),
            facts=QdrantFactVectorStore(qdrant_client),
            admin=QdrantVectorIndexAdmin(qdrant_client),
            # Owned by `Clients`, which closes it. Left out here so shutting the registry
            # down does not close a connection the rest of the process is still using.
            client=None,
            ping=qdrant_client.get_collections,
        )
    }

    if settings.chroma_url:
        built["chroma"] = await _chroma_backend(settings, live=BindingLiveCollections(cached))

    return VectorBackends(built, cached, default=settings.default_vector_backend)


async def _chroma_backend(settings: Settings, *, live: LiveCollections) -> Backend:
    try:
        import chromadb
    except ModuleNotFoundError as error:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "CHROMA_URL is set but the chroma client is not installed. "
            "Install this package with the 'chroma' extra, or unset CHROMA_URL."
        ) from error

    import httpx

    from app.services.vector_chroma import (
        ChromaFactVectorStore,
        ChromaVectorIndexAdmin,
        ChromaVectorStore,
    )

    parsed = httpx.URL(settings.chroma_url or "")
    client = await chromadb.AsyncHttpClient(
        host=parsed.host,
        port=parsed.port or (443 if parsed.scheme == "https" else 8000),
        ssl=parsed.scheme == "https",
        tenant=settings.chroma_tenant,
        database=settings.chroma_database,
    )
    return Backend(
        kind="chroma",
        store=ChromaVectorStore(client, live=live),
        facts=ChromaFactVectorStore(client),
        admin=ChromaVectorIndexAdmin(client, live=live),
        client=client,
        # `heartbeat`, not a collection listing: Chroma's listing returns every collection
        # on the server, which is one per tenant and would make the readiness probe's cost
        # grow with the customer count.
        ping=client.heartbeat,
        pointer_backed=True,
    )


def single_backend(
    *,
    store: VectorStore,
    facts: FactVectorStore,
    admin: VectorIndexAdmin,
    kind: str = "qdrant",
    bindings: VectorBindingStore | None = None,
) -> VectorBackends:
    """One backend and no database, for tests and for anything that wires by hand."""
    return VectorBackends(
        {kind: Backend(kind=kind, store=store, facts=facts, admin=admin)},
        bindings if bindings is not None else MemoryVectorBindingStore(),
        default=kind,
    )


def check_grace_period(seconds: float) -> None:
    """Refuse a migration grace period shorter than the binding cache's TTL.

    The zero-downtime argument depends on it. A replica holding a cached binding still
    reads the *source* backend for up to :data:`BINDING_CACHE_SECONDS` after a promotion,
    and dropping the source inside that window turns a stale read into an empty one — a
    retrieval outage for a fraction of traffic, which under ``fail_open`` is invisible.
    """
    if seconds <= BINDING_CACHE_SECONDS:
        raise ValueError(
            f"the migration grace period ({seconds}s) must exceed the binding cache TTL "
            f"({BINDING_CACHE_SECONDS}s), or a replica can read a collection that has "
            "already been dropped"
        )


__all__ = [
    "BACKEND_KINDS",
    "BINDING_CACHE_SECONDS",
    "Backend",
    "BindingLiveCollections",
    "CachedBindings",
    "Page",
    "RoutingFactVectorStore",
    "RoutingVectorIndexAdmin",
    "RoutingVectorStore",
    "UnknownBackend",
    "VectorBackends",
    "build_backends",
    "check_grace_period",
    "enabled_kinds",
    "single_backend",
]
