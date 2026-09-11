"""Qdrant, behind the three vector ports.

All three implementations live in one module rather than each beside its own protocol,
and that is the point of the module: **no port module imports a vendor client.** Before
task 19 the ports and their only implementation shared a file, which cost nothing while
there was one backend and would have cost a great deal with two — the first Chroma
implementation would have been written by reading a Qdrant one in the same scroll, and
every accidental assumption in the port would have been invisible.

What that reveals, now that the seam is drawn:

* :func:`vector_size` parses a Qdrant ``CollectionInfo``. It is not a port concept and no
  longer sits in one.
* Collection *versioning* is Qdrant's aliases. The port asks for
  :meth:`~app.services.vector_index.VectorIndexAdmin.live_collection` and
  ``promote``; how a backend answers those is its own business, and here the answer is an
  alias operation.
* The payload indexes are created here because Qdrant needs them created. A backend that
  indexes metadata automatically satisfies the same contract with a no-op, and the
  contract is "these filters are fast", not "call this method".

``qdrant_client`` is imported at module scope. It was deferred inside methods while these
classes lived in the port modules — importing a client on the strength of a protocol was
worth avoiding — and that reason is gone: nothing imports this module unless it is going
to talk to Qdrant.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from qdrant_client import models

from app.services.fact_vectors import (
    DEFAULT_SEARCH_LIMIT as FACT_SEARCH_LIMIT,
)
from app.services.fact_vectors import (
    MEMORY_INDEXED_PAYLOAD_FIELDS,
    SCROLL_BATCH,
    FactPoint,
    memory_collection_for,
)
from app.services.vector_index import COPY_BATCH, FIRST_VERSION, Page, logical_for, versioned
from app.services.vector_store import (
    DEFAULT_SEARCH_LIMIT,
    INDEXED_PAYLOAD_FIELDS,
    ChunkPoint,
    Match,
    Stored,
    collection_for,
)

logger = logging.getLogger(__name__)


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
    return models.Filter(
        must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))]
    )


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


class QdrantVectorStore:
    """The production document index, over the client `/readyz` already probes."""

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
        a collection under the alias name — indexed before that task — is left exactly as
        it is: promoting it costs a gap in retrieval, and the only thing worth paying that
        for is a reindex, which does it deliberately.
        """
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

    async def replace_document(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, points: Sequence[ChunkPoint]
    ) -> None:
        await self.upsert(organization_id, points)
        name = collection_for(organization_id)
        if not await self._present(name):
            return
        # The document's points that are not among the ones just written: a filter on
        # the document plus `must_not has_id`, which Qdrant evaluates server-side, so a
        # document of a thousand chunks is one request rather than a scroll and a diff.
        await self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchValue(value=str(document_id))
                        )
                    ],
                    must_not=(
                        [models.HasIdCondition(has_id=[point.id for point in points])]
                        if points
                        else None
                    ),
                )
            ),
            wait=True,
        )

    async def delete_connector(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        await self._delete_by(organization_id, "connector_id", str(connector_id))

    async def delete_points(self, organization_id: uuid.UUID, ids: Sequence[str]) -> None:
        name = collection_for(organization_id)
        if not ids or not await self._present(name):
            return
        await self._client.delete(
            collection_name=name,
            points_selector=models.PointIdsList(points=list(ids)),
            wait=True,
        )

    async def _delete_by(self, organization_id: uuid.UUID, key: str, value: str) -> None:
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
            # which will be thrown away". A backend without a threshold has to reproduce
            # that meaning itself; the contract suite pins it.
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


# ---------------------------------------------------------------------------
# index administration
# ---------------------------------------------------------------------------


class QdrantVectorIndexAdmin:
    """The collection-addressing operations, over the same client the store uses."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def live_collection(self, organization_id: uuid.UUID) -> str | None:
        alias = logical_for(organization_id)
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
        if not await self._client.collection_exists(collection):
            return None
        return vector_size(await self._client.get_collection(collection))

    async def count_points(self, collection: str) -> int:
        if not await self._client.collection_exists(collection):
            return 0
        result = await self._client.count(collection_name=collection, exact=True)
        return int(result.count)

    async def scroll(
        self, collection: str, *, cursor: str | None, limit: int, with_vectors: bool = False
    ) -> Page:
        if not await self._client.collection_exists(collection):
            return Page(points=(), cursor=None)
        points, offset = await self._client.scroll(
            collection_name=collection,
            offset=cursor,
            limit=limit,
            with_payload=True,
            with_vectors=with_vectors,
        )
        return Page(
            points=tuple(
                ChunkPoint(
                    id=str(point.id),
                    vector=_vector_of(point) if with_vectors else [],
                    payload=dict(point.payload or {}),
                )
                for point in points
            ),
            cursor=str(offset) if offset is not None else None,
        )

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None:
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

    async def promote(self, organization_id: uuid.UUID, collection: str) -> None:
        """Make ``collection`` the one the tenant's reads resolve to — an alias swap.

        Atomic when an alias already exists. The one exception is the first promotion of
        a collection created before aliases existed, which Qdrant cannot alias over.
        """
        alias = logical_for(organization_id)
        if await self._alias_target(alias) is None and await self._client.collection_exists(alias):
            # The one-way promotion described in `app.services.vector_index`. Dropping
            # first is unavoidable: Qdrant refuses an alias whose name a collection
            # already holds.
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


def _vector_of(point: Any) -> list[float]:
    """A scrolled point's vector, for the unnamed-vector collections this build makes.

    Qdrant returns ``None`` when vectors were not requested and a mapping for a
    collection configured with named vectors. Neither is what a migration copies, so both
    become an empty list — and the count check at the end of a copy is what catches it if
    that ever silently happens.
    """
    vector = getattr(point, "vector", None)
    if isinstance(vector, list):
        return [float(value) for value in vector]
    return []


# ---------------------------------------------------------------------------
# conversation memory
# ---------------------------------------------------------------------------


def _for_end_user(end_user_id: uuid.UUID) -> Any:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="end_user_id", match=models.MatchValue(value=str(end_user_id))
            )
        ]
    )


class QdrantFactVectorStore:
    """The production memory index."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._ensured: set[str] = set()
        self._widths: dict[str, int] = {}

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        name = memory_collection_for(organization_id)
        if name in self._ensured:
            return
        if not await self._client.collection_exists(name):
            await self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
            )
            for path in MEMORY_INDEXED_PAYLOAD_FIELDS:
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
        limit: int = FACT_SEARCH_LIMIT,
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


__all__ = [
    "QdrantFactVectorStore",
    "QdrantVectorIndexAdmin",
    "QdrantVectorStore",
    "vector_size",
]
