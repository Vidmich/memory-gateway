"""The conversation-memory index: ``org_{org_id}_memory`` (SPEC §6.4, step 4).

A second collection, and a second port, rather than a ``kind`` argument threaded through
:mod:`app.services.vector_store`. The two indexes look alike and behave differently in
every way that matters: documents are filtered by connector and read back by document,
facts are filtered by end user and read back by id; documents are written by a worker in
batches of hundreds, facts one at a time from the control plane; a document's chunk is
replaced by re-ingesting the file, a fact is replaced by superseding a row. One protocol
covering both would be a union of two filter vocabularies where every caller passes half
of it as ``None``.

**The vectors here are an index, not the record.** Text, confidence, supersession and
expiry live in ``memory_facts``; a point carries only what a *filter* needs. That split is
the whole reason recall can be trusted: a payload claiming ``superseded: false`` about a
row that says otherwise would be a memory leak of exactly the kind this feature must not
have, and the only way to make that impossible is for the payload to have no opinion.
Recall therefore searches here for ids and reads the rows from PostgreSQL — one round trip
each, concurrently, and the database is the single source of truth about what is still
believed.

**Point ids are the fact ids.** No UUIDv5 derivation, unlike the document store: a fact is
one point, so its own id is already unique and already deterministic, and using it means
deleting a fact is a delete-by-id rather than a delete-by-filter.

``end_user_id`` gets a payload index, because it is on every read and every erasure and
without one both are a full scan of the organization's memory.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.vector_store import Match, cosine, vector_size

logger = logging.getLogger(__name__)

#: SPEC §6.4. Same per-tenant naming as the document collection, and for the same reason:
#: a query that reaches the wrong collection finds nothing rather than somebody else's.
MEMORY_COLLECTION_TEMPLATE = "org_{organization_id}_memory"

#: Equality filters used on every read and every delete.
INDEXED_PAYLOAD_FIELDS = ("end_user_id",)

DEFAULT_SEARCH_LIMIT = 16

#: Points per scroll page when the sweeper enumerates a collection. Ids only, so a page
#: is small however large the payloads are.
SCROLL_BATCH = 512


def memory_collection_for(organization_id: uuid.UUID) -> str:
    return MEMORY_COLLECTION_TEMPLATE.format(organization_id=organization_id)


@dataclass(frozen=True, slots=True)
class FactPoint:
    """One fact's vector, plus the payload SPEC §6.4 names.

    ``text`` is deliberately absent. See the module docstring: the row is the record.
    """

    id: str
    vector: list[float]
    payload: dict[str, Any]


def fact_payload(
    *,
    organization_id: uuid.UUID,
    end_user_id: uuid.UUID,
    kind: str,
    confidence: float,
    created_at: float,
) -> dict[str, Any]:
    """SPEC §6.4's payload, built in one place so both implementations agree on it."""
    return {
        "org_id": str(organization_id),
        "end_user_id": str(end_user_id),
        "kind": kind,
        "confidence": float(confidence),
        "created_at": float(created_at),
    }


class FactVectorStore(Protocol):
    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None: ...

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[FactPoint]) -> None: ...

    async def delete(self, organization_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]) -> None:
        """Remove specific facts. Used by an edit that changes the text, by a delete, and
        by supersession — a superseded fact keeps its row and loses its vector, so recall
        cannot reach it even if a later filter is written wrongly."""
        ...

    async def delete_end_user(self, organization_id: uuid.UUID, end_user_id: uuid.UUID) -> None:
        """Every point belonging to one end user. The erasure path (SPEC §6.5), by filter
        rather than by the ids the caller thinks exist — a purge that missed a point
        nobody remembered would be a right-to-erasure failure."""
        ...

    async def drop(self, organization_id: uuid.UUID) -> None: ...

    async def dimension(self, organization_id: uuid.UUID) -> int | None: ...

    async def ids(self, organization_id: uuid.UUID) -> set[str]:
        """Every point id in this tenant's memory collection.

        Only the orphan sweeper asks. It exists because a vector whose row is gone is a
        fact that still influences answers after it was deleted — the one failure that
        cannot be seen from either store alone, which is exactly why the comparison has to
        be made from outside both.
        """
        ...

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        end_user_id: uuid.UUID,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        """Similar facts for one end user.

        ``end_user_id`` is required rather than optional, and that is the isolation rule
        SPEC §6.1 states: conversation memory is private to ``(organization, end_user)``.
        The collection already provides the first half; a keyword argument with no default
        is what makes the second half impossible to forget.
        """
        ...

    async def count(
        self, organization_id: uuid.UUID, *, end_user_id: uuid.UUID | None = None
    ) -> int: ...


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


class QdrantFactVectorStore:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._ensured: set[str] = set()
        self._widths: dict[str, int] = {}

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        from qdrant_client import models

        name = memory_collection_for(organization_id)
        if name in self._ensured:
            return
        if not await self._client.collection_exists(name):
            await self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
            for path in INDEXED_PAYLOAD_FIELDS:
                await self._client.create_payload_index(
                    collection_name=name,
                    field_name=path,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            logger.info(
                "created memory collection", extra={"collection": name, "dimension": dimension}
            )
        self._ensured.add(name)

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[FactPoint]) -> None:
        from qdrant_client import models

        if not points:
            return
        await self._client.upsert(
            collection_name=memory_collection_for(organization_id),
            points=[
                models.PointStruct(id=point.id, vector=point.vector, payload=point.payload)
                for point in points
            ],
            # The control plane returns the created fact to a browser that will list it
            # again immediately; an unacknowledged write would show a fact that recall
            # cannot yet find.
            wait=True,
        )

    async def delete(self, organization_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]) -> None:
        from qdrant_client import models

        if not fact_ids:
            return
        name = memory_collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return
        await self._client.delete(
            collection_name=name,
            points_selector=models.PointIdsList(points=[str(value) for value in fact_ids]),
            wait=True,
        )

    async def delete_end_user(self, organization_id: uuid.UUID, end_user_id: uuid.UUID) -> None:
        from qdrant_client import models

        name = memory_collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return
        await self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(filter=_for_end_user(end_user_id)),
            wait=True,
        )

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = memory_collection_for(organization_id)
        self._ensured.discard(name)
        self._widths.pop(name, None)
        if await self._client.collection_exists(name):
            await self._client.delete_collection(name)

    async def ids(self, organization_id: uuid.UUID) -> set[str]:
        name = memory_collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return set()
        found: set[str] = set()
        offset: Any = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=name,
                offset=offset,
                limit=SCROLL_BATCH,
                with_payload=False,
                with_vectors=False,
            )
            found.update(str(point.id) for point in points)
            if offset is None:
                return found

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        name = memory_collection_for(organization_id)
        if (cached := self._widths.get(name)) is not None:
            return cached
        if not await self._client.collection_exists(name):
            return None
        info = await self._client.get_collection(name)
        size = vector_size(info)
        if size is not None:
            self._widths[name] = size
        return size

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        end_user_id: uuid.UUID,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        name = memory_collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return []
        found = await self._client.query_points(
            collection_name=name,
            query=list(vector),
            query_filter=_for_end_user(end_user_id),
            limit=limit,
            score_threshold=min_score or None,
            with_payload=True,
        )
        return [
            Match(id=str(point.id), score=float(point.score), payload=dict(point.payload or {}))
            for point in found.points
        ]

    async def count(
        self, organization_id: uuid.UUID, *, end_user_id: uuid.UUID | None = None
    ) -> int:
        name = memory_collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return 0
        result = await self._client.count(
            collection_name=name,
            count_filter=None if end_user_id is None else _for_end_user(end_user_id),
            exact=True,
        )
        return int(result.count)


def _for_end_user(end_user_id: uuid.UUID) -> Any:
    from qdrant_client import models

    return models.Filter(
        must=[
            models.FieldCondition(
                key="end_user_id", match=models.MatchValue(value=str(end_user_id))
            )
        ]
    )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryFactVectorStore:
    """Brute-force cosine over a dict, exactly as the document store's twin does."""

    collections: dict[str, dict[str, FactPoint]] = field(default_factory=dict)
    dimensions: dict[str, int] = field(default_factory=dict)

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        name = memory_collection_for(organization_id)
        self.collections.setdefault(name, {})
        self.dimensions[name] = dimension

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[FactPoint]) -> None:
        name = memory_collection_for(organization_id)
        if name not in self.collections:
            raise KeyError(f"collection {name} does not exist")
        expected = self.dimensions[name]
        for point in points:
            if len(point.vector) != expected:
                raise ValueError(
                    f"vector has {len(point.vector)} dimensions, collection expects {expected}"
                )
            self.collections[name][point.id] = point

    async def delete(self, organization_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]) -> None:
        points = self.collections.get(memory_collection_for(organization_id))
        if points is None:
            return
        for value in fact_ids:
            points.pop(str(value), None)

    async def delete_end_user(self, organization_id: uuid.UUID, end_user_id: uuid.UUID) -> None:
        points = self.collections.get(memory_collection_for(organization_id))
        if points is None:
            return
        for identifier in [
            key
            for key, point in points.items()
            if str(point.payload.get("end_user_id")) == str(end_user_id)
        ]:
            del points[identifier]

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = memory_collection_for(organization_id)
        self.collections.pop(name, None)
        self.dimensions.pop(name, None)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        return self.dimensions.get(memory_collection_for(organization_id))

    async def ids(self, organization_id: uuid.UUID) -> set[str]:
        return set(self.collections.get(memory_collection_for(organization_id), {}))

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        end_user_id: uuid.UUID,
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        points = self.collections.get(memory_collection_for(organization_id), {})
        scored = [
            Match(id=point.id, score=cosine(vector, point.vector), payload=dict(point.payload))
            for point in points.values()
            if str(point.payload.get("end_user_id")) == str(end_user_id)
        ]
        scored = [match for match in scored if match.score >= min_score]
        scored.sort(key=lambda match: (-match.score, match.id))
        return scored[:limit]

    async def count(
        self, organization_id: uuid.UUID, *, end_user_id: uuid.UUID | None = None
    ) -> int:
        points = self.collections.get(memory_collection_for(organization_id), {})
        if end_user_id is None:
            return len(points)
        return sum(
            1
            for point in points.values()
            if str(point.payload.get("end_user_id")) == str(end_user_id)
        )


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "INDEXED_PAYLOAD_FIELDS",
    "MEMORY_COLLECTION_TEMPLATE",
    "FactPoint",
    "FactVectorStore",
    "MemoryFactVectorStore",
    "QdrantFactVectorStore",
    "fact_payload",
    "memory_collection_for",
]
