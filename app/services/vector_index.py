"""Collection *names*, and the operations a reindex needs on top of the ordinary port.

Everything that reads or writes vectors addresses one **logical name** per tenant —
``org_{org_id}_docs`` — and the collection actually serving it carries a version:
``org_{org_id}_docs_v1``, ``_v2``, and so on. Nothing outside this module and
:mod:`app.services.reindex` needs to know that, which is the point.

**Why the indirection exists before anything needs it.** A reindex builds a whole new
collection with a new vector width and then makes it live. Without the indirection,
"make it live" is delete-then-rename, and no backend here has an atomic cross-collection
rename — so it is delete-then-rebuild, during which every search returns nothing. It was
introduced in task 17, on an index that was usually empty, precisely because retrofitting
it costs a gap in retrieval and the moment you want it is an urgent embedding-model
migration. That is the wrong time to be discovering this.

**How a backend answers ``live_collection`` and ``promote`` is its own business.** Task 17
wrote both in terms of Qdrant aliases, because Qdrant was the only backend and its alias
is genuinely the right mechanism there — one atomic operation. Chroma has no aliases, and
resolves the same question from a row this deployment keeps. The port asks *which
collection is live* and *make this one live*; it does not ask for an alias, and a port
method named after one vendor's feature is how the next implementation ends up emulating
that feature instead of satisfying the contract.

Each backend also owns its own non-atomic edge, and must document it where it lives. For
Qdrant it is the first promotion of a pre-alias collection, which cannot be aliased over
and so is dropped first — once, on an index that has just been rebuilt beside the live
one, measured in milliseconds rather than in the length of a re-embedding run.

**Reads go through the logical name; the admin operations address collections directly.**
That split is deliberate. A search must follow whatever is live right now, while a reindex
must be able to write into a collection that is *not* live and read from one that is.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.services.vector_store import ChunkPoint, Match, MemoryVectorStore, collection_for

logger = logging.getLogger(__name__)

#: Where a fresh tenant's first collection starts. Version 0 is reserved for "a
#: collection named exactly like the logical name", which is what a tenant indexed
#: before task 17 has.
FIRST_VERSION = 1

_VERSION = re.compile(r"_v(\d+)$")

#: Points copied per round trip during a reindex. Large enough that a big corpus is not a
#: million requests, small enough that one page fits comfortably in memory alongside the
#: embeddings being computed for it.
COPY_BATCH = 256


def logical_for(organization_id: uuid.UUID) -> str:
    """The name everything reads through. Identical to
    :func:`~app.services.vector_store.collection_for` — the indirection is not a new
    naming scheme, it is an extra level under the same name."""
    return collection_for(organization_id)


def versioned(organization_id: uuid.UUID, version: int) -> str:
    return f"{logical_for(organization_id)}_v{version}"


def version_of(collection: str) -> int:
    """The version encoded in a collection name; ``0`` for a name without one.

    Zero rather than ``None`` so the successor of an unversioned legacy collection is
    version 1 without a branch at the call site.
    """
    found = _VERSION.search(collection)
    return int(found.group(1)) if found else 0


def successor(organization_id: uuid.UUID, current: str | None) -> str:
    """The collection a reindex should build next for this tenant."""
    return versioned(organization_id, version_of(current or "") + 1)


class LiveCollections(Protocol):
    """Where a backend that cannot answer "which collection is live" keeps the answer.

    Qdrant needs none of this: an alias *is* the pointer, it is stored beside the data,
    and moving it is one atomic operation. Chroma has no equivalent, so the pointer has to
    live somewhere this deployment controls — a row, in practice — and the alternatives
    are worse in ways worth recording:

    * **Rename the collections.** Two renames with a gap between them, during which the
      logical name resolves to nothing and every search returns empty. That is exactly the
      outage the indirection exists to prevent.
    * **A marker in each collection's own metadata.** Resolution becomes "list every
      collection on the server and scan", which is a full listing on the retrieval path
      and grows with the number of tenants rather than staying constant.

    The cost of a row is that a promotion is visible to other replicas only when they next
    read it, so an implementation that caches must bound that staleness — and the
    degradation is benign: a replica reading a stale pointer searches the *previous*
    collection, which still exists until the grace period expires and still holds valid
    results. Neither is ever "no collection".
    """

    async def resolve(self, organization_id: uuid.UUID) -> str | None:
        """The collection this tenant's reads go to, or ``None`` if there is no pointer."""
        ...

    async def point(self, organization_id: uuid.UUID, collection: str) -> None: ...

    async def forget(self, organization_id: uuid.UUID) -> None:
        """Drop the pointer. Offboarding — the collection itself is deleted separately."""
        ...


@dataclass
class InMemoryLiveCollections:
    """A dict. What the contract suite runs against, and what a single-process test uses."""

    pointers: dict[uuid.UUID, str] = field(default_factory=dict)

    async def resolve(self, organization_id: uuid.UUID) -> str | None:
        return self.pointers.get(organization_id)

    async def point(self, organization_id: uuid.UUID, collection: str) -> None:
        self.pointers[organization_id] = collection

    async def forget(self, organization_id: uuid.UUID) -> None:
        self.pointers.pop(organization_id, None)


@dataclass(frozen=True, slots=True)
class Page:
    """One scroll page: the points, and where to resume."""

    points: tuple[ChunkPoint, ...]
    cursor: str | None


class VectorIndexAdmin(Protocol):
    """The operations that address a collection rather than a tenant.

    A second protocol rather than more methods on
    :class:`~app.services.vector_store.VectorStore`, because the ordinary port's whole job
    is that a caller cannot name a collection — that is what makes a cross-tenant read
    impossible to write by accident. These take a collection name because a reindex has
    two of them in hand at once, and the isolation argument does not apply to a job whose
    scope *is* the platform.
    """

    async def live_collection(self, organization_id: uuid.UUID) -> str | None:
        """The collection this tenant's reads currently resolve to, or ``None`` when
        nothing has been indexed. How the backend knows is the backend's business."""
        ...

    async def create_collection(self, collection: str, *, dimension: int) -> None: ...

    async def drop_collection(self, collection: str) -> None: ...

    async def collection_dimension(self, collection: str) -> int | None: ...

    async def count_points(self, collection: str) -> int: ...

    async def scroll(
        self, collection: str, *, cursor: str | None, limit: int, with_vectors: bool = False
    ) -> Page:
        """A page of points with their payloads, and their vectors only if asked.

        Default off, because a **reindex** re-embeds from the payload text: pulling the
        old vectors down would be the largest part of the transfer and every byte of it
        would be discarded. A **migration between backends** is the opposite case — same
        model, same width, so the vectors are exactly what is being moved and
        re-embedding them would be an expense with no effect.

        ``cursor`` is opaque and belongs to the backend that issued it: a point id for
        one, an offset for another. The consequence of the second kind is worth stating
        rather than discovering — paging by offset over a collection being written to can
        skip or repeat a point, which is why the count check at the end of a copy is
        load-bearing rather than belt-and-braces.
        """
        ...

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None: ...

    async def search_in(
        self, collection: str, vector: Sequence[float], *, limit: int = 5
    ) -> list[Match]:
        """The sample search a reindex runs before it promotes. Against the *new*
        collection by name, because it is not live yet and the ordinary port can only
        reach what is."""
        ...

    async def promote(self, organization_id: uuid.UUID, collection: str) -> None:
        """Make ``collection`` the one this tenant's reads resolve to.

        Must be atomic from a reader's point of view: a search running throughout sees
        the old collection or the new one and never neither. Each backend documents its
        own exception where it implements this, and there is exactly one — Qdrant's first
        promotion of a collection created before aliases existed.
        """
        ...

    async def documents_in(self, collection: str) -> set[str]:
        """Distinct ``document_id`` payload values. What the orphan sweeper compares
        against the ``documents`` table."""
        ...

    async def list_collections(self) -> list[str]:
        """Every collection name the server holds.

        Only the sweeper asks, and it asks so that a collection a reindex built and then
        superseded can be found and dropped. Those are invisible from every other angle:
        nothing points at them, so nothing notices them except the disk.
        """
        ...


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryVectorIndexAdmin:
    """The same operations over a :class:`~app.services.vector_store.MemoryVectorStore`.

    Shares the store's dictionaries rather than copying them, which is what makes a test
    able to assert that a search through the *logical name* returns what the reindex wrote
    into the *new collection* — the property the whole indirection exists for.
    """

    store: MemoryVectorStore

    async def live_collection(self, organization_id: uuid.UUID) -> str | None:
        name = self.store.live(organization_id)
        return name if name in self.store.collections else None

    async def create_collection(self, collection: str, *, dimension: int) -> None:
        self.store.collections.setdefault(collection, {})
        self.store.dimensions[collection] = dimension

    async def drop_collection(self, collection: str) -> None:
        self.store.collections.pop(collection, None)
        self.store.dimensions.pop(collection, None)

    async def collection_dimension(self, collection: str) -> int | None:
        return self.store.dimensions.get(collection)

    async def count_points(self, collection: str) -> int:
        return len(self.store.collections.get(collection, {}))

    async def scroll(
        self, collection: str, *, cursor: str | None, limit: int, with_vectors: bool = False
    ) -> Page:
        points = self.store.collections.get(collection, {})
        # Sorted so paging is stable, the way a real scroll's ordering by point id is.
        ordered = [points[key] for key in sorted(points)]
        start = 0 if cursor is None else _index_after(sorted(points), cursor)
        window = ordered[start : start + limit]
        following = start + len(window)
        return Page(
            points=tuple(
                ChunkPoint(
                    id=point.id,
                    vector=list(point.vector) if with_vectors else [],
                    payload=dict(point.payload),
                )
                for point in window
            ),
            cursor=window[-1].id if following < len(ordered) and window else None,
        )

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None:
        held = self.store.collections.setdefault(collection, {})
        expected = self.store.dimensions.get(collection)
        for point in points:
            if expected is not None and len(point.vector) != expected:
                # Every real backend refuses this, so the double has to as well: a
                # reindex that wrote old-width vectors into the new collection is exactly
                # the bug the verification step exists to catch, and a permissive double
                # would let it reach the promotion.
                raise ValueError(
                    f"vector has {len(point.vector)} dimensions, "
                    f"collection {collection} expects {expected}"
                )
            held[point.id] = point

    async def search_in(
        self, collection: str, vector: Sequence[float], *, limit: int = 5
    ) -> list[Match]:
        from app.services.vector_store import cosine

        points = self.store.collections.get(collection, {})
        scored = [
            Match(id=point.id, score=cosine(vector, point.vector), payload=dict(point.payload))
            for point in points.values()
        ]
        scored.sort(key=lambda match: (-match.score, match.id))
        return scored[:limit]

    async def promote(self, organization_id: uuid.UUID, collection: str) -> None:
        self.store.live_collections[logical_for(organization_id)] = collection

    async def list_collections(self) -> list[str]:
        return sorted(self.store.collections)

    async def documents_in(self, collection: str) -> set[str]:
        return {
            str(point.payload["document_id"])
            for point in self.store.collections.get(collection, {}).values()
            if point.payload.get("document_id") is not None
        }


def _index_after(ordered: Sequence[str], cursor: str) -> int:
    try:
        return ordered.index(cursor) + 1
    except ValueError:
        # A cursor for a point that has since been deleted. Starting over is correct
        # rather than merely safe: every upsert in a reindex is keyed by a deterministic
        # id, so redoing a page costs time and changes nothing.
        return 0


__all__ = [
    "COPY_BATCH",
    "FIRST_VERSION",
    "InMemoryLiveCollections",
    "LiveCollections",
    "MemoryVectorIndexAdmin",
    "Page",
    "VectorIndexAdmin",
    "logical_for",
    "successor",
    "version_of",
    "versioned",
]
