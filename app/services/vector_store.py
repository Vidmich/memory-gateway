"""The vector index, behind a port.

One Qdrant collection per organization, ``org_{org_id}_docs`` (SPEC §9.4). A collection
per tenant rather than one collection with an ``org_id`` filter, and the reason is the
same one that makes SPEC §5.3 a hard rule: a filter that is *forgotten* returns another
customer's documents, whereas a collection that is not named cannot be read at all. It
also makes offboarding a drop rather than a delete-by-filter over a live index.

Point ids are **deterministic** — a UUIDv5 over ``(document_id, chunk_index)``. Re-running
ingestion for a document therefore overwrites its points instead of adding a second copy,
which is what makes the pipeline safe to retry and what makes two workers racing the same
document converge rather than double it.

Determinism is not enough on its own, though. A document that was 40 chunks and is now
30 leaves points 30 to 39 behind, still matching queries with text that no longer exists in
the file. So re-ingestion **deletes by ``document_id`` first**, then upserts. Both halves
are needed; either alone is a bug that only shows up after an edit.

:class:`MemoryVectorStore` implements the same protocol with brute-force cosine, and
``tests/vector_store_contract.py`` runs one set of assertions against both. The Qdrant
half of that run needs a real server — payload filters and delete-by-filter are precisely
where a hand-written double would agree with itself and disagree with Qdrant.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: SPEC §9.4. The tenant id is in the name, so a query that reaches the wrong collection
#: is a typo that finds nothing rather than a leak.
COLLECTION_TEMPLATE = "org_{organization_id}_docs"

#: Namespace for deterministic point ids. A fixed constant — generating it per process,
#: or deriving it from anything mutable, would make yesterday's points unreachable.
POINT_NAMESPACE = uuid.UUID("6f4d1f4c-1f6a-5f9a-9a4e-6a1f0d2c3b45")

#: Payload fields that get an index. Both are equality filters used on every read and
#: every delete; without indexes those are full scans of the collection.
INDEXED_PAYLOAD_FIELDS = ("connector_id", "document_id")

DEFAULT_SEARCH_LIMIT = 10


def collection_for(organization_id: uuid.UUID) -> str:
    return COLLECTION_TEMPLATE.format(organization_id=organization_id)


def point_id(document_id: uuid.UUID, chunk_index: int) -> str:
    """The id of one chunk, forever."""
    return str(uuid.uuid5(POINT_NAMESPACE, f"{document_id}:{chunk_index}"))


@dataclass(frozen=True, slots=True)
class ChunkPoint:
    id: str
    vector: list[float]
    #: SPEC §9.3's chunk metadata, plus the text itself — retrieval has to return
    #: something a prompt can carry, and a second round trip to fetch it would double the
    #: latency of every request in task 10.
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Match:
    id: str
    score: float
    payload: dict[str, Any]

    @property
    def text(self) -> str:
        return str(self.payload.get("text", ""))


class VectorStore(Protocol):
    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        """Create the collection if it is missing, with payload indexes. Idempotent, and
        called on every ingestion rather than once at provisioning: a collection dropped
        by hand should heal on the next upload, not need an operator."""
        ...

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None: ...

    async def delete_document(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
        """Every point belonging to one document, by filter — not by the ids we think it
        has. A document that shrank has points whose ids the caller no longer knows."""
        ...

    async def delete_connector(
        self, organization_id: uuid.UUID, connector_id: uuid.UUID
    ) -> None: ...

    async def drop(self, organization_id: uuid.UUID) -> None:
        """Remove the whole collection. Offboarding, and nothing else."""
        ...

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]: ...

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int: ...


# ---------------------------------------------------------------------------
# Qdrant
# ---------------------------------------------------------------------------


class QdrantVectorStore:
    """The production implementation, over the client `/readyz` already probes."""

    def __init__(self, client: Any) -> None:
        self._client = client
        #: Collections this process has already ensured. A cache of a *creation*, not of
        #: data: the worst case for a stale entry is an upsert against a collection
        #: somebody deleted, which fails loudly and is retried.
        self._ensured: set[str] = set()

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        from qdrant_client import models

        name = collection_for(organization_id)
        if name in self._ensured:
            return
        if not await self._client.collection_exists(name):
            await self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=dimension,
                    # Cosine, per SPEC §9.4. Embeddings are direction, not magnitude; dot
                    # product would rank long chunks above relevant ones.
                    distance=models.Distance.COSINE,
                ),
            )
            for path in INDEXED_PAYLOAD_FIELDS:
                await self._client.create_payload_index(
                    collection_name=name,
                    field_name=path,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            logger.info(
                "created vector collection", extra={"collection": name, "dimension": dimension}
            )
        self._ensured.add(name)

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None:
        from qdrant_client import models

        if not points:
            return
        await self._client.upsert(
            collection_name=collection_for(organization_id),
            points=[
                models.PointStruct(id=point.id, vector=point.vector, payload=point.payload)
                for point in points
            ],
            # The next step marks the document `indexed`, and doing that before the write
            # has landed would tell a customer their file is searchable when it is not.
            wait=True,
        )

    async def delete_document(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
        await self._delete_by(organization_id, "document_id", str(document_id))

    async def delete_connector(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        await self._delete_by(organization_id, "connector_id", str(connector_id))

    async def _delete_by(self, organization_id: uuid.UUID, key: str, value: str) -> None:
        from qdrant_client import models

        name = collection_for(organization_id)
        if not await self._client.collection_exists(name):
            # Nothing was ever indexed for this tenant. A delete asks for an end state,
            # and that end state already holds.
            return
        await self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(filter=_equals(key, value)),
            wait=True,
        )

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = collection_for(organization_id)
        self._ensured.discard(name)
        if await self._client.collection_exists(name):
            await self._client.delete_collection(name)

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        from qdrant_client import models

        name = collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return []

        query_filter = None
        if connector_ids:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="connector_id",
                        match=models.MatchAny(any=[str(value) for value in connector_ids]),
                    )
                ]
            )

        found = await self._client.query_points(
            collection_name=name,
            query=list(vector),
            query_filter=query_filter,
            limit=limit,
            # Pushed down rather than filtered afterwards, so `limit` means "this many
            # results above the threshold" instead of "this many candidates, some of
            # which will be thrown away".
            score_threshold=min_score or None,
            with_payload=True,
        )
        return [
            Match(id=str(point.id), score=float(point.score), payload=dict(point.payload or {}))
            for point in found.points
        ]

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int:
        name = collection_for(organization_id)
        if not await self._client.collection_exists(name):
            return 0
        query_filter = None
        if document_id is not None:
            query_filter = _equals("document_id", str(document_id))
        elif connector_id is not None:
            query_filter = _equals("connector_id", str(connector_id))
        result = await self._client.count(
            collection_name=name, count_filter=query_filter, exact=True
        )
        return int(result.count)


def _equals(key: str, value: str) -> Any:
    from qdrant_client import models

    return models.Filter(
        must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))]
    )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryVectorStore:
    """Brute-force cosine over a dict. Exact, which is what a test wants."""

    collections: dict[str, dict[str, ChunkPoint]] = field(default_factory=dict)
    dimensions: dict[str, int] = field(default_factory=dict)

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        name = collection_for(organization_id)
        self.collections.setdefault(name, {})
        self.dimensions[name] = dimension

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None:
        name = collection_for(organization_id)
        if name not in self.collections:
            raise KeyError(f"collection {name} does not exist")
        expected = self.dimensions[name]
        for point in points:
            if len(point.vector) != expected:
                # Qdrant refuses this too. Accepting it here would let a dimension bug
                # pass every test and fail only in production.
                raise ValueError(
                    f"vector has {len(point.vector)} dimensions, collection expects {expected}"
                )
            self.collections[name][point.id] = point

    async def delete_document(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self._delete_where(organization_id, "document_id", str(document_id))

    async def delete_connector(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        self._delete_where(organization_id, "connector_id", str(connector_id))

    def _delete_where(self, organization_id: uuid.UUID, key: str, value: str) -> None:
        points = self.collections.get(collection_for(organization_id))
        if points is None:
            return
        for identifier in [pid for pid, p in points.items() if str(p.payload.get(key)) == value]:
            del points[identifier]

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = collection_for(organization_id)
        self.collections.pop(name, None)
        self.dimensions.pop(name, None)

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        points = self.collections.get(collection_for(organization_id), {})
        wanted = {str(value) for value in connector_ids}
        scored = [
            Match(id=point.id, score=_cosine(vector, point.vector), payload=dict(point.payload))
            for point in points.values()
            if not wanted or str(point.payload.get("connector_id")) in wanted
        ]
        scored = [match for match in scored if match.score >= min_score]
        scored.sort(key=lambda match: (-match.score, match.id))
        return scored[:limit]

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int:
        points = self.collections.get(collection_for(organization_id), {})
        if document_id is not None:
            return sum(
                1 for p in points.values() if str(p.payload.get("document_id")) == str(document_id)
            )
        if connector_id is not None:
            return sum(
                1
                for p in points.values()
                if str(p.payload.get("connector_id")) == str(connector_id)
            )
        return len(points)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = [
    "COLLECTION_TEMPLATE",
    "DEFAULT_SEARCH_LIMIT",
    "INDEXED_PAYLOAD_FIELDS",
    "ChunkPoint",
    "Match",
    "MemoryVectorStore",
    "QdrantVectorStore",
    "VectorStore",
    "collection_for",
    "point_id",
]
