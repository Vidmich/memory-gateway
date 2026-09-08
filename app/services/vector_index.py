"""Collection *names*, and the operations a reindex needs on top of the ordinary port.

Everything that reads or writes vectors addresses one name per tenant —
``org_{org_id}_docs``. Since task 17 that name is an **alias**, and the collection behind
it carries a version: ``org_{org_id}_docs_v1``, ``_v2``, and so on. Nothing outside this
module and :mod:`app.services.reindex` needs to know that, which is the point.

**Why the indirection exists before anything needs it.** A reindex builds a whole new
collection with a new vector width and then makes it live. Without an alias, "make it
live" is delete-then-rename, and Qdrant has no rename — so it is delete-then-rebuild,
during which every search returns nothing. With an alias it is one atomic operation. The
alias is introduced now, on an index that is usually empty, precisely because retrofitting
it costs a gap in retrieval and the moment you want it is an urgent embedding-model
migration. That is the wrong time to be discovering this.

**The one non-atomic moment is the first promotion, and it is one-way.** A collection
created before this module existed is literally named ``org_{id}_docs``, and Qdrant will
not let an alias take a name a collection already holds. Promoting it means dropping the
old collection and then creating the alias — two operations with a gap between them.
Every *subsequent* swap is alias-to-alias and atomic. So the gap happens once, on an index
that has just been rebuilt beside the live one, and it is measured in milliseconds rather
than in the length of a re-embedding run.

**Reads go through the alias name; the admin operations address collections directly.**
That split is deliberate. A search must follow whatever is live right now, while a reindex
must be able to write into a collection that is *not* live and read from one that is.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.services.vector_store import ChunkPoint, Match, MemoryVectorStore, collection_for

logger = logging.getLogger(__name__)

#: Where a fresh tenant's first collection starts. Version 0 is reserved for "a collection
#: named like the alias", which is what a pre-task-17 tenant has.
FIRST_VERSION = 1

_VERSION = re.compile(r"_v(\d+)$")

#: Points copied per round trip during a reindex. Large enough that a big corpus is not a
#: million requests, small enough that one page fits comfortably in memory alongside the
#: embeddings being computed for it.
COPY_BATCH = 256


def alias_for(organization_id: uuid.UUID) -> str:
    """The name everything reads through. Identical to
    :func:`~app.services.vector_store.collection_for` — an alias is not a new naming
    scheme, it is an extra level under the same name."""
    return collection_for(organization_id)


def versioned(organization_id: uuid.UUID, version: int) -> str:
    return f"{alias_for(organization_id)}_v{version}"


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
        """What the alias currently points at, or the legacy collection, or ``None``."""
        ...

    async def create_collection(self, collection: str, *, dimension: int) -> None: ...

    async def drop_collection(self, collection: str) -> None: ...

    async def collection_dimension(self, collection: str) -> int | None: ...

    async def count_points(self, collection: str) -> int: ...

    async def scroll(self, collection: str, *, cursor: str | None, limit: int) -> Page:
        """A page of points **with their payloads and no vectors**.

        No vectors on purpose: a reindex re-embeds from the payload text, so pulling the
        old vectors down would be the largest part of the transfer and every byte of it
        would be discarded.
        """
        ...

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None: ...

    async def search_in(
        self, collection: str, vector: Sequence[float], *, limit: int = 5
    ) -> list[Match]:
        """The sample search a reindex runs before it swaps. Against the *new* collection
        by name, because it is not live yet and therefore has no alias to search."""
        ...

    async def swap_alias(self, organization_id: uuid.UUID, collection: str) -> None:
        """Point the tenant's alias at ``collection``.

        Atomic when an alias already exists. See the module docstring for the one case
        that is not — a legacy collection holding the alias name.
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
# qdrant
# ---------------------------------------------------------------------------


class QdrantVectorIndexAdmin:
    """The production implementation, over the same client the store uses."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def live_collection(self, organization_id: uuid.UUID) -> str | None:
        alias = alias_for(organization_id)
        found = await self._alias_target(alias)
        if found is not None:
            return found
        # No alias. Either a legacy collection under the alias name, or nothing indexed.
        return alias if await self._client.collection_exists(alias) else None

    async def _alias_target(self, alias: str) -> str | None:
        try:
            described = await self._client.get_aliases()
        except Exception:
            # An older server, or a transient failure. Treated as "no alias", which
            # degrades to the legacy path rather than to an exception on a read.
            logger.warning("could not list qdrant aliases", exc_info=True)
            return None
        for entry in getattr(described, "aliases", ()) or ():
            if getattr(entry, "alias_name", None) == alias:
                name: str = entry.collection_name
                return name
        return None

    async def create_collection(self, collection: str, *, dimension: int) -> None:
        from qdrant_client import models

        from app.services.vector_store import INDEXED_PAYLOAD_FIELDS

        if await self._client.collection_exists(collection):
            # A resumed reindex finds the collection it created before it was killed.
            # Reusing it is what makes resumption cheap: the points already copied are
            # still there, and the upserts that follow are keyed by the same ids.
            return
        await self._client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
        )
        for path in INDEXED_PAYLOAD_FIELDS:
            await self._client.create_payload_index(
                collection_name=collection,
                field_name=path,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def drop_collection(self, collection: str) -> None:
        if await self._client.collection_exists(collection):
            await self._client.delete_collection(collection)

    async def collection_dimension(self, collection: str) -> int | None:
        from app.services.vector_store import vector_size

        if not await self._client.collection_exists(collection):
            return None
        return vector_size(await self._client.get_collection(collection))

    async def count_points(self, collection: str) -> int:
        if not await self._client.collection_exists(collection):
            return 0
        result = await self._client.count(collection_name=collection, exact=True)
        return int(result.count)

    async def scroll(self, collection: str, *, cursor: str | None, limit: int) -> Page:
        if not await self._client.collection_exists(collection):
            return Page(points=(), cursor=None)
        points, offset = await self._client.scroll(
            collection_name=collection,
            offset=cursor,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return Page(
            points=tuple(
                ChunkPoint(id=str(point.id), vector=[], payload=dict(point.payload or {}))
                for point in points
            ),
            cursor=str(offset) if offset is not None else None,
        )

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None:
        from qdrant_client import models

        if not points:
            return
        await self._client.upsert(
            collection_name=collection,
            points=[
                models.PointStruct(id=point.id, vector=point.vector, payload=point.payload)
                for point in points
            ],
            wait=True,
        )

    async def search_in(
        self, collection: str, vector: Sequence[float], *, limit: int = 5
    ) -> list[Match]:
        found = await self._client.query_points(
            collection_name=collection, query=list(vector), limit=limit, with_payload=True
        )
        return [
            Match(id=str(point.id), score=float(point.score), payload=dict(point.payload or {}))
            for point in found.points
        ]

    async def swap_alias(self, organization_id: uuid.UUID, collection: str) -> None:
        from qdrant_client import models

        alias = alias_for(organization_id)
        if await self._alias_target(alias) is None and await self._client.collection_exists(alias):
            # The one-way promotion described in the module docstring. Dropping first is
            # unavoidable: Qdrant refuses an alias whose name a collection already holds.
            logger.info(
                "promoting a pre-alias collection; searches are unavailable for the "
                "duration of the swap",
                extra={"collection": alias},
            )
            await self._client.delete_collection(alias)
        await self._client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
                )
            ]
        )

    async def list_collections(self) -> list[str]:
        described = await self._client.get_collections()
        return sorted(entry.name for entry in getattr(described, "collections", ()) or ())

    async def documents_in(self, collection: str) -> set[str]:
        found: set[str] = set()
        cursor: str | None = None
        while True:
            page = await self.scroll(collection, cursor=cursor, limit=COPY_BATCH)
            for point in page.points:
                value = point.payload.get("document_id")
                if value is not None:
                    found.add(str(value))
            cursor = page.cursor
            if cursor is None:
                return found


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryVectorIndexAdmin:
    """The same operations over a :class:`~app.services.vector_store.MemoryVectorStore`.

    Shares the store's dictionaries rather than copying them, which is what makes a test
    able to assert that a search through the *alias* returns what the reindex wrote into
    the *new collection* — the property the whole indirection exists for.
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

    async def scroll(self, collection: str, *, cursor: str | None, limit: int) -> Page:
        points = self.store.collections.get(collection, {})
        # Sorted so paging is stable, the way a real scroll's ordering by point id is.
        ordered = [points[key] for key in sorted(points)]
        start = 0 if cursor is None else _index_after(sorted(points), cursor)
        window = ordered[start : start + limit]
        following = start + len(window)
        return Page(
            points=tuple(
                ChunkPoint(id=point.id, vector=[], payload=dict(point.payload)) for point in window
            ),
            cursor=window[-1].id if following < len(ordered) and window else None,
        )

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None:
        held = self.store.collections.setdefault(collection, {})
        expected = self.store.dimensions.get(collection)
        for point in points:
            if expected is not None and len(point.vector) != expected:
                # Qdrant refuses this, so the double has to as well: a reindex that wrote
                # old-width vectors into the new collection is exactly the bug the
                # verification step exists to catch, and a permissive double would let it
                # reach the swap.
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

    async def swap_alias(self, organization_id: uuid.UUID, collection: str) -> None:
        self.store.aliases[alias_for(organization_id)] = collection

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
    "MemoryVectorIndexAdmin",
    "Page",
    "QdrantVectorIndexAdmin",
    "VectorIndexAdmin",
    "alias_for",
    "successor",
    "version_of",
    "versioned",
]
