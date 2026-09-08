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


@dataclass(frozen=True, slots=True)
class Stored:
    """One indexed chunk, read back by *identity* rather than by similarity.

    Separate from :class:`Match` because it has no score, and a score of zero would be a
    number somebody eventually renders. What this answers is "what is actually in the
    index for this document", which is the first question when a file reports ``indexed``
    and its answers are still wrong.
    """

    id: str
    payload: dict[str, Any]

    @property
    def text(self) -> str:
        return str(self.payload.get("text", ""))

    @property
    def index(self) -> int:
        return int(self.payload.get("chunk_index", 0))


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

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        """The vector width this tenant's collection was built with, or ``None`` when
        there is no collection yet.

        Retrieval asks so it can refuse to search an index built by a different embedding
        model. Without it a changed ``EMBEDDING_DIMENSION`` is a silent failure: Qdrant
        rejects a mismatched query vector with a message about dimensions, which under
        ``fail_open`` becomes "the model answered without its documents" on every request
        and nothing anywhere says why.
        """
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

    async def chunks(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, *, limit: int = 500
    ) -> list[Stored]:
        """One document's chunks, in the order they were cut.

        The chunk inspector, and the fastest way to see whether extraction produced text
        worth embedding: a PDF whose every chunk is a page header, a spreadsheet indexed
        as bare cells, a Word file that came out as its pre-review draft. All three report
        ``indexed`` and answer badly, and nothing else on the screen distinguishes them.
        """
        ...

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
        #: Vector width per collection — see :meth:`dimension`.
        self._widths: dict[str, int] = {}

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        """Create the tenant's first collection and its alias, if there is neither.

        Since task 17 the name every other method uses is an *alias*, so this creates
        ``org_{id}_docs_v1`` and points ``org_{id}_docs`` at it. A tenant that already has
        a collection under the alias name — indexed before this task — is left exactly as
        it is: promoting it costs a gap in retrieval, and the only thing worth paying that
        for is a reindex, which does it deliberately. See
        :mod:`app.services.vector_index`.
        """
        from app.services.vector_index import FIRST_VERSION, versioned

        name = collection_for(organization_id)
        if name in self._ensured:
            return
        if not await self._present(name):
            physical = versioned(organization_id, FIRST_VERSION)
            await self._create(physical, dimension=dimension)
            await self._point_alias(name, physical)
            logger.info(
                "created vector collection",
                extra={"collection": physical, "alias": name, "dimension": dimension},
            )
        self._ensured.add(name)

    async def _create(self, collection: str, *, dimension: int) -> None:
        from qdrant_client import models

        await self._client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(
                size=dimension,
                # Cosine, per SPEC §9.4. Embeddings are direction, not magnitude; dot
                # product would rank long chunks above relevant ones.
                distance=models.Distance.COSINE,
            ),
        )
        for path in INDEXED_PAYLOAD_FIELDS:
            await self._client.create_payload_index(
                collection_name=collection,
                field_name=path,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    async def _point_alias(self, alias: str, collection: str) -> None:
        from qdrant_client import models

        await self._client.update_collection_aliases(
            change_aliases_operations=[
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
                )
            ]
        )

    async def _present(self, name: str) -> bool:
        """Whether ``name`` resolves to anything — a collection, or an alias for one.

        Every read below guards on this rather than on ``collection_exists`` alone.
        Qdrant's existence check answers about *collections*, so a tenant whose data sits
        behind an alias would otherwise read as "nothing indexed" and every search would
        quietly return no documents.
        """
        if await self._client.collection_exists(name):
            return True
        try:
            described = await self._client.get_aliases()
        except Exception:
            logger.warning("could not list qdrant aliases", exc_info=True)
            return False
        return any(
            getattr(entry, "alias_name", None) == name
            for entry in getattr(described, "aliases", ()) or ()
        )

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
        if not await self._present(name):
            # Nothing was ever indexed for this tenant. A delete asks for an end state,
            # and that end state already holds.
            return
        await self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(filter=_equals(key, value)),
            wait=True,
        )

    async def drop(self, organization_id: uuid.UUID) -> None:
        from app.services.vector_index import QdrantVectorIndexAdmin

        name = collection_for(organization_id)
        self._ensured.discard(name)
        self._widths.pop(name, None)
        # Through the admin so the *collection* behind the alias goes, not just the
        # alias: deleting an alias leaves the vectors in place, which for an
        # offboarding is the one outcome that must not happen.
        live = await QdrantVectorIndexAdmin(self._client).live_collection(organization_id)
        if live is not None:
            await self._client.delete_collection(live)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        """One round trip per collection per process, then a dictionary lookup.

        Cached because it is read on the request path and a collection's width cannot
        change without the collection being recreated — which happens through
        :meth:`drop`, in this process for a delete and in the worker for a reindex. The
        stale case is therefore a *worker* recreating a collection this process has
        already seen, and it costs one request's retrieval before the resulting error
        clears the entry.
        """
        name = collection_for(organization_id)
        if (cached := self._widths.get(name)) is not None:
            return cached
        if not await self._present(name):
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
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        from qdrant_client import models

        name = collection_for(organization_id)
        if not await self._present(name):
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

    async def chunks(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, *, limit: int = 500
    ) -> list[Stored]:
        name = collection_for(organization_id)
        if not await self._present(name):
            return []
        # `scroll`, not `query_points`: this is a filtered read of everything matching,
        # with no vector to score against. Ordering is done here rather than pushed down
        # because Qdrant orders a scroll by point id, and the ids are hashes.
        points, _ = await self._client.scroll(
            collection_name=name,
            scroll_filter=_equals("document_id", str(document_id)),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        found = [Stored(id=str(point.id), payload=dict(point.payload or {})) for point in points]
        found.sort(key=lambda chunk: chunk.index)
        return found

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int:
        name = collection_for(organization_id)
        if not await self._present(name):
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


def vector_size(info: Any) -> int | None:
    """The width out of a Qdrant ``CollectionInfo``, whichever shape it is in.

    A collection can be configured with a single unnamed vector or a mapping of named
    ones. This build only ever creates the first, but reading the second rather than
    raising means a collection somebody made by hand reports a width instead of breaking
    every request through the gateway that reads it.
    """
    params = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    if vectors is None:
        return None
    if isinstance(vectors, dict):
        sizes = {getattr(value, "size", None) for value in vectors.values()}
        return next(iter(sizes)) if len(sizes) == 1 else None
    size = getattr(vectors, "size", None)
    return int(size) if size is not None else None


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
    """Brute-force cosine over a dict. Exact, which is what a test wants.

    Since task 17 it models the alias indirection too: ``collections`` is keyed by the
    *physical* name and ``aliases`` maps the name callers use onto it. Without that a
    reindex would pass here — two dictionary keys, swapped — and fail against Qdrant,
    where the swap is the only part that is hard.
    """

    collections: dict[str, dict[str, ChunkPoint]] = field(default_factory=dict)
    dimensions: dict[str, int] = field(default_factory=dict)
    #: Alias name to collection name. Populated by ``ensure_collection`` and by a swap.
    aliases: dict[str, str] = field(default_factory=dict)

    def live(self, organization_id: uuid.UUID) -> str:
        """The collection the tenant's alias resolves to right now.

        Falls back to the alias name itself, which is both the pre-alias shape and what
        makes a store somebody filled in by hand behave the way it always did.
        """
        alias = collection_for(organization_id)
        return self.aliases.get(alias, alias)

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        alias = collection_for(organization_id)
        name = self.aliases.get(alias)
        if name is None:
            name = alias if alias in self.collections else f"{alias}_v1"
            self.aliases[alias] = name
        self.collections.setdefault(name, {})
        self.dimensions[name] = dimension

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None:
        name = self.live(organization_id)
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
        points = self.collections.get(self.live(organization_id))
        if points is None:
            return
        for identifier in [pid for pid, p in points.items() if str(p.payload.get(key)) == value]:
            del points[identifier]

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = self.live(organization_id)
        self.collections.pop(name, None)
        self.dimensions.pop(name, None)
        self.aliases.pop(collection_for(organization_id), None)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        return self.dimensions.get(self.live(organization_id))

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        points = self.collections.get(self.live(organization_id), {})
        wanted = {str(value) for value in connector_ids}
        scored = [
            Match(id=point.id, score=cosine(vector, point.vector), payload=dict(point.payload))
            for point in points.values()
            if not wanted or str(point.payload.get("connector_id")) in wanted
        ]
        scored = [match for match in scored if match.score >= min_score]
        scored.sort(key=lambda match: (-match.score, match.id))
        return scored[:limit]

    async def chunks(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, *, limit: int = 500
    ) -> list[Stored]:
        points = self.collections.get(self.live(organization_id), {})
        found = [
            Stored(id=point.id, payload=dict(point.payload))
            for point in points.values()
            if str(point.payload.get("document_id")) == str(document_id)
        ]
        found.sort(key=lambda chunk: chunk.index)
        return found[:limit]

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int:
        points = self.collections.get(self.live(organization_id), {})
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


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
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
    "Stored",
    "VectorStore",
    "collection_for",
    "cosine",
    "point_id",
    "vector_size",
]
