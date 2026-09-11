"""Chroma, behind the same three vector ports.

The point of a second backend is not Chroma. It is that a port with one implementation is
a hypothesis: every assumption in it has been free so far because the only thing on the
other side agreed with itself. What follows is the list of places where this one did not
agree, and what each of them cost.

**Distances, not scores.** Chroma answers with cosine *distance* — ``1 - similarity``,
never negative. The port's contract is a similarity where higher is better, so every
result is converted here. This is the classic silent bug in a backend swap: getting it
backwards raises nothing, ranks the worst matches first with total confidence, and shows
up only as answers that quietly get vaguer. ``tests/vector_store_contract.py`` pins it
with one-hot vectors so the assertion is exact rather than approximately true.

**No score threshold, so ``limit`` has to be honoured by over-fetching.** Qdrant applies
``min_score`` server-side, which makes ``limit`` mean "this many results above the floor".
Chroma has no such parameter, so a naive implementation would ask for ``limit`` candidates
and then discard some of them, returning fewer than the caller asked for. Here the query
asks for :data:`OVERFETCH` times as many and truncates after filtering.

**No aliases.** Which collection is live is a question Chroma cannot answer about itself;
see :class:`~app.services.vector_index.LiveCollections` for where the answer lives instead
and why the alternatives are worse. Promotion is therefore a pointer write, which is
atomic from a reader's point of view — the property the port actually asks for.

**No declared dimension.** Chroma infers a collection's width from its first insert and
will not tell you what it inferred. The width is written into the collection's metadata at
creation and read back from there, because ``dimension()`` returning ``None`` for a
collection that exists is the same silent failure a missing width was: retrieval loses its
ability to refuse an index built by a different embedding model, and under ``fail_open``
that is "the model answered without its documents" on every request, with nothing saying
why.

**No payload indexes to create.** Chroma indexes metadata itself, so the port's
``ensure_collection`` contract is satisfied with no equivalent call. The contract was
never "create these indexes"; it is "these filters are cheap", and that one still needs
measuring on a real corpus rather than assuming.

**The default embedding function is a trap.** Chroma's clients default to an ONNX model
downloaded on first use. Every collection here is created and opened with
``embedding_function=None``: in task 18's image the download fails (no egress, non-root,
read-only filesystem), and if it ever succeeded it would embed queries with a model that
has nothing to do with the one that built the index — a worse outcome than the failure.
:data:`NO_EMBEDDING_FUNCTION` is passed explicitly everywhere, and a test asserts it
reaches the client on every call rather than trusting that it does.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.fact_vectors import (
    DEFAULT_SEARCH_LIMIT as FACT_SEARCH_LIMIT,
)
from app.services.fact_vectors import (
    FactPoint,
    memory_collection_for,
)
from app.services.vector_index import (
    FIRST_VERSION,
    LiveCollections,
    Page,
    logical_for,
    versioned,
)
from app.services.vector_store import (
    DEFAULT_SEARCH_LIMIT,
    ChunkPoint,
    Match,
    Stored,
)

logger = logging.getLogger(__name__)

#: Collection metadata key holding the vector width — see the module docstring.
DIMENSION_KEY = "gateway_dimension"

#: Collection metadata key naming the payload fields that were ``None`` when the point was
#: written. Chroma accepts a null metadata value and does not promise to give it back, and
#: an omitted key and a key whose value is ``None`` are different things to
#: ``page_or_section``: one means "this chunk has no section", the other means the store
#: lost a field. Recording the absentees keeps the round trip exact without this module
#: having to know which fields the payload contains.
ABSENT_KEY = "gateway_absent"

#: How many extra candidates to fetch when a score floor has to be applied client-side.
#: Four is a guess with a stated shape: the floor is a *similarity* cutoff and the results
#: are ranked, so the qualifying ones are a prefix — over-fetching only matters when the
#: prefix is longer than ``limit``, and a factor rather than a constant keeps the cost
#: proportional to what was asked for. The cap stops a large ``limit`` from turning into an
#: unbounded read.
OVERFETCH = 4
MAX_FETCH = 1000

#: Page size when enumerating a collection by offset. Chroma pages with limit/offset rather
#: than a resumable cursor; see :meth:`ChromaVectorIndexAdmin.scroll`.
PAGE = 256

#: Cosine, per SPEC §9.4, and stated rather than defaulted. Chroma's default space is L2,
#: which for embeddings — direction, not magnitude — would rank by vector length and be
#: wrong in a way that still returns plausible-looking results.
_CONFIGURATION: dict[str, Any] = {"hnsw": {"space": "cosine"}}


#: Passed to every ``create_collection`` and ``get_collection`` call in this module.
#:
#: Named rather than written as a bare ``None`` at four call sites, so the intent is
#: greppable and a reviewer sees a decision instead of an omission. The vectors in this
#: system are produced by :mod:`app.services.embeddings` under a platform-wide model
#: (SPEC §9.4); a backend computing its own would be a second, undeclared embedding model
#: in a system whose entire design is that there is exactly one.
NO_EMBEDDING_FUNCTION: Any = None


def _similarity(distance: float) -> float:
    """Chroma's cosine distance as the port's cosine similarity.

    Clamped, because floating-point error puts an identical vector a hair either side of
    distance 0 and a caller comparing against ``min_score=1.0`` should not see 1.0000001.
    """
    return max(-1.0, min(1.0, 1.0 - float(distance)))


def _where_equals(key: str, value: str) -> dict[str, Any]:
    return {key: {"$eq": value}}


def _where_any(key: str, values: Sequence[str]) -> dict[str, Any]:
    return {key: {"$in": list(values)}}


def _encode(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """A payload as Chroma stores it: the text as the document, the rest as metadata.

    ``text`` becomes the document rather than another metadata field because that is what
    the field is for, and because it keeps the metadata small — the part every filter
    scans. Nulls are dropped and recorded; see :data:`ABSENT_KEY`.
    """
    metadata: dict[str, Any] = {}
    absent: list[str] = []
    text = ""
    for key, value in payload.items():
        if key == "text":
            text = "" if value is None else str(value)
            continue
        if value is None:
            absent.append(key)
            continue
        metadata[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
    if absent:
        metadata[ABSENT_KEY] = ",".join(sorted(absent))
    return text, metadata


def _decode(document: str | None, metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """The inverse of :func:`_encode`, exact for every payload this system writes."""
    held = dict(metadata or {})
    absent = str(held.pop(ABSENT_KEY, "") or "")
    held.pop(DIMENSION_KEY, None)
    payload: dict[str, Any] = dict(held)
    for key in filter(None, absent.split(",")):
        payload[key] = None
    if document is not None:
        payload["text"] = document
    return payload


def _rows(result: Mapping[str, Any]) -> list[tuple[str, str | None, dict[str, Any]]]:
    """A ``GetResult`` as ``(id, document, metadata)`` triples.

    Chroma returns parallel lists and omits the ones that were not included, so every
    access is guarded. A result with fewer documents than ids is not a case to paper over:
    it would silently produce chunks with empty text, which reads on screen as a document
    that extracted to nothing.
    """
    ids = list(result.get("ids") or [])
    documents = list(result.get("documents") or [])
    metadatas = list(result.get("metadatas") or [])
    rows: list[tuple[str, str | None, dict[str, Any]]] = []
    for index, identifier in enumerate(ids):
        document = documents[index] if index < len(documents) else None
        metadata = metadatas[index] if index < len(metadatas) else {}
        rows.append((str(identifier), document, dict(metadata or {})))
    return rows


def _hits(result: Mapping[str, Any]) -> list[Match]:
    """A ``QueryResult``'s first (and only) query, as matches with similarities."""
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    matches: list[Match] = []
    for index, identifier in enumerate(ids):
        document = documents[index] if index < len(documents) else None
        metadata = metadatas[index] if index < len(metadatas) else {}
        distance = distances[index] if index < len(distances) else 1.0
        matches.append(
            Match(
                id=str(identifier),
                score=_similarity(distance),
                payload=_decode(document, metadata),
            )
        )
    return matches


def _fetch_size(limit: int, *, filtered: bool) -> int:
    """How many candidates to ask for to return ``limit`` after a client-side floor."""
    if not filtered:
        return limit
    return min(MAX_FETCH, max(limit, limit * OVERFETCH))


class _Collections:
    """Opening collections, with the not-found case as a value rather than an exception.

    Every read in both stores begins "if this tenant has nothing indexed, the answer is
    empty" — a delete asks for an end state that already holds, a search finds nothing.
    Chroma raises for a missing collection, so that shape would otherwise be a try/except
    around a dozen call sites, and one of them would eventually swallow a real error.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def open(self, name: str) -> Any | None:
        from chromadb.errors import NotFoundError

        try:
            return await self._client.get_collection(
                name=name, embedding_function=NO_EMBEDDING_FUNCTION
            )
        except NotFoundError:
            return None
        except ValueError as error:
            # Older servers answer a missing collection with a plain ValueError. Narrowed
            # by message rather than caught wholesale, so a genuine argument error still
            # surfaces instead of being read as "nothing indexed".
            if "does not exist" in str(error).lower():
                return None
            raise

    async def create(self, name: str, *, dimension: int) -> Any:
        return await self._client.get_or_create_collection(
            name=name,
            configuration=_CONFIGURATION,
            metadata={DIMENSION_KEY: int(dimension)},
            embedding_function=NO_EMBEDDING_FUNCTION,
        )

    async def drop(self, name: str) -> None:
        from chromadb.errors import NotFoundError

        try:
            await self._client.delete_collection(name=name)
        except NotFoundError:
            return
        except ValueError as error:
            if "does not exist" not in str(error).lower():
                raise


def _dimension_of(collection: Any) -> int | None:
    metadata = getattr(collection, "metadata", None) or {}
    value = metadata.get(DIMENSION_KEY)
    return int(value) if value is not None else None


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


class ChromaVectorStore:
    """The document index over Chroma."""

    def __init__(self, client: Any, *, live: LiveCollections) -> None:
        self._collections = _Collections(client)
        self._live = live
        #: Logical names this process has already ensured — a cache of a *creation*, the
        #: same one the Qdrant store keeps and for the same reason.
        self._ensured: set[str] = set()

    async def _open_live(self, organization_id: uuid.UUID) -> Any | None:
        name = await self._live.resolve(organization_id)
        if name is None:
            return None
        return await self._collections.open(name)

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        logical = logical_for(organization_id)
        if logical in self._ensured:
            return
        if await self._live.resolve(organization_id) is None:
            physical = versioned(organization_id, FIRST_VERSION)
            await self._collections.create(physical, dimension=dimension)
            await self._live.point(organization_id, physical)
            logger.info(
                "created vector collection",
                extra={"collection": physical, "logical": logical, "dimension": dimension},
            )
        self._ensured.add(logical)

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None:
        if not points:
            return
        collection = await self._open_live(organization_id)
        if collection is None:
            raise KeyError(f"no live collection for organization {organization_id}")
        encoded = [_encode(point.payload) for point in points]
        await collection.upsert(
            ids=[point.id for point in points],
            embeddings=[list(point.vector) for point in points],
            documents=[text for text, _ in encoded],
            metadatas=[metadata for _, metadata in encoded],
        )

    async def delete_document(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
        await self._delete_where(organization_id, _where_equals("document_id", str(document_id)))

    async def replace_document(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, points: Sequence[ChunkPoint]
    ) -> None:
        await self.upsert(organization_id, points)
        collection = await self._open_live(organization_id)
        if collection is None:
            return
        # Chroma's `where` has no "not one of these ids", so the document's ids are read
        # and the difference deleted by id. Two requests, both bounded by one document.
        found = await collection.get(
            where=_where_equals("document_id", str(document_id)), include=[]
        )
        keep = {point.id for point in points}
        stale = [identifier for identifier in (found.get("ids") or []) if identifier not in keep]
        if stale:
            await collection.delete(ids=stale)

    async def delete_connector(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        await self._delete_where(organization_id, _where_equals("connector_id", str(connector_id)))

    async def delete_points(self, organization_id: uuid.UUID, ids: Sequence[str]) -> None:
        collection = await self._open_live(organization_id)
        if collection is None or not ids:
            return
        await collection.delete(ids=list(ids))

    async def _delete_where(self, organization_id: uuid.UUID, where: dict[str, Any]) -> None:
        collection = await self._open_live(organization_id)
        if collection is None:
            # Nothing was ever indexed. A delete asks for an end state that already holds.
            return
        await collection.delete(where=where)

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = await self._live.resolve(organization_id)
        self._ensured.discard(logical_for(organization_id))
        if name is not None:
            await self._collections.drop(name)
        await self._live.forget(organization_id)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        collection = await self._open_live(organization_id)
        return None if collection is None else _dimension_of(collection)

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = DEFAULT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        collection = await self._open_live(organization_id)
        if collection is None:
            return []
        where = (
            _where_any("connector_id", [str(value) for value in connector_ids])
            if connector_ids
            else None
        )
        # The connector filter goes *down* into the query rather than being applied to the
        # results. Filtering afterwards would make `limit` count candidates instead of
        # answers, and a gateway pointed at one connector would silently retrieve fewer
        # chunks the more traffic its neighbours had.
        found = await collection.query(
            query_embeddings=[list(vector)],
            n_results=_fetch_size(limit, filtered=min_score > 0.0),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        matches = [match for match in _hits(found) if match.score >= min_score]
        return matches[:limit]

    async def chunks(
        self, organization_id: uuid.UUID, document_id: uuid.UUID, *, limit: int = 500
    ) -> list[Stored]:
        collection = await self._open_live(organization_id)
        if collection is None:
            return []
        found = await collection.get(
            where=_where_equals("document_id", str(document_id)),
            limit=limit,
            include=["documents", "metadatas"],
        )
        stored = [
            Stored(id=identifier, payload=_decode(document, metadata))
            for identifier, document, metadata in _rows(found)
        ]
        # Ordered here rather than pushed down, exactly as the Qdrant store does: a
        # filtered read has no ordering worth asking a vector database for, and the ids
        # are hashes.
        stored.sort(key=lambda chunk: chunk.index)
        return stored

    async def count(
        self,
        organization_id: uuid.UUID,
        *,
        connector_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> int:
        collection = await self._open_live(organization_id)
        if collection is None:
            return 0
        where: dict[str, Any] | None = None
        if document_id is not None:
            where = _where_equals("document_id", str(document_id))
        elif connector_id is not None:
            where = _where_equals("connector_id", str(connector_id))
        if where is None:
            return int(await collection.count())
        # No filtered count in Chroma, so this reads the ids and lengths them. Bounded by
        # one connector or one document rather than by the collection, which is what the
        # two callers actually ask about.
        found = await collection.get(where=where, include=[])
        return len(list(found.get("ids") or []))


# ---------------------------------------------------------------------------
# index administration
# ---------------------------------------------------------------------------


class ChromaVectorIndexAdmin:
    """The collection-addressing operations, over the same client and pointer."""

    def __init__(self, client: Any, *, live: LiveCollections) -> None:
        self._client = client
        self._collections = _Collections(client)
        self._live = live

    async def live_collection(self, organization_id: uuid.UUID) -> str | None:
        name = await self._live.resolve(organization_id)
        if name is None:
            return None
        # A pointer to a collection somebody deleted by hand is "nothing indexed", not a
        # crash on every read. Healing it is `ensure_collection`'s job.
        return name if await self._collections.open(name) is not None else None

    async def create_collection(self, collection: str, *, dimension: int) -> None:
        # `get_or_create`, so a resumed reindex finds the collection it created before it
        # was killed and keeps the points already copied.
        await self._collections.create(collection, dimension=dimension)

    async def drop_collection(self, collection: str) -> None:
        await self._collections.drop(collection)

    async def collection_dimension(self, collection: str) -> int | None:
        opened = await self._collections.open(collection)
        return None if opened is None else _dimension_of(opened)

    async def count_points(self, collection: str) -> int:
        opened = await self._collections.open(collection)
        return 0 if opened is None else int(await opened.count())

    async def scroll(
        self, collection: str, *, cursor: str | None, limit: int, with_vectors: bool = False
    ) -> Page:
        """A page, addressed by offset rather than by a resumable cursor.

        Chroma has no scroll cursor, so the opaque string the port promises is an offset
        this module encodes and decodes. The consequence is worth naming: a page taken
        while the collection is being written to can skip or repeat a point, whereas a
        cursor keyed by point id cannot. Nothing here writes to a collection it is copying
        *from*, and the count check at the end of every copy is what would catch it if
        that ever changed — which is why that check verifies exactly rather than
        approximately.
        """
        opened = await self._collections.open(collection)
        if opened is None:
            return Page(points=(), cursor=None)
        offset = int(cursor) if cursor else 0
        include = ["documents", "metadatas"] + (["embeddings"] if with_vectors else [])
        found = await opened.get(limit=limit, offset=offset, include=include)
        vectors = list(found.get("embeddings") or []) if with_vectors else []
        points = tuple(
            ChunkPoint(
                id=identifier,
                vector=[float(value) for value in vectors[index]]
                if with_vectors and index < len(vectors)
                else [],
                payload=_decode(document, metadata),
            )
            for index, (identifier, document, metadata) in enumerate(_rows(found))
        )
        following = offset + len(points)
        return Page(points=points, cursor=str(following) if len(points) == limit else None)

    async def upsert_into(self, collection: str, points: Sequence[ChunkPoint]) -> None:
        if not points:
            return
        opened = await self._collections.open(collection)
        if opened is None:
            raise KeyError(f"collection {collection} does not exist")
        encoded = [_encode(point.payload) for point in points]
        await opened.upsert(
            ids=[point.id for point in points],
            embeddings=[list(point.vector) for point in points],
            documents=[text for text, _ in encoded],
            metadatas=[metadata for _, metadata in encoded],
        )

    async def search_in(
        self, collection: str, vector: Sequence[float], *, limit: int = 5
    ) -> list[Match]:
        opened = await self._collections.open(collection)
        if opened is None:
            return []
        found = await opened.query(
            query_embeddings=[list(vector)],
            n_results=limit,
            include=["documents", "metadatas", "distances"],
        )
        return _hits(found)

    async def promote(self, organization_id: uuid.UUID, collection: str) -> None:
        """One pointer write, and therefore atomic from a reader's point of view.

        No exception to document, unlike Qdrant's first promotion of a pre-alias
        collection: there is no name for a collection to be holding hostage.
        """
        await self._live.point(organization_id, collection)

    async def list_collections(self) -> list[str]:
        found = await self._client.list_collections()
        return sorted(
            str(entry) if isinstance(entry, str) else str(getattr(entry, "name", entry))
            for entry in found or ()
        )

    async def documents_in(self, collection: str) -> set[str]:
        opened = await self._collections.open(collection)
        if opened is None:
            return set()
        found: set[str] = set()
        offset = 0
        while True:
            page = await opened.get(limit=PAGE, offset=offset, include=["metadatas"])
            rows = _rows(page)
            for _, _, metadata in rows:
                value = metadata.get("document_id")
                if value is not None:
                    found.add(str(value))
            if len(rows) < PAGE:
                return found
            offset += len(rows)


# ---------------------------------------------------------------------------
# conversation memory
# ---------------------------------------------------------------------------


class ChromaFactVectorStore:
    """The memory index over Chroma.

    No pointer and no versions: ``org_{id}_memory`` is addressed directly, exactly as it
    is against Qdrant. A reindex rebuilds document chunks, and facts are re-embedded in
    place from the rows that own them, so there has never been a second collection to
    swap between.
    """

    def __init__(self, client: Any) -> None:
        self._collections = _Collections(client)
        self._ensured: set[str] = set()

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        name = memory_collection_for(organization_id)
        if name in self._ensured:
            return
        await self._collections.create(name, dimension=dimension)
        self._ensured.add(name)

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[FactPoint]) -> None:
        if not points:
            return
        collection = await self._collections.open(memory_collection_for(organization_id))
        if collection is None:
            raise KeyError(f"no memory collection for organization {organization_id}")
        encoded = [_encode(point.payload) for point in points]
        await collection.upsert(
            ids=[point.id for point in points],
            embeddings=[list(point.vector) for point in points],
            metadatas=[metadata for _, metadata in encoded],
        )

    async def delete(self, organization_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]) -> None:
        if not fact_ids:
            return
        collection = await self._collections.open(memory_collection_for(organization_id))
        if collection is None:
            return
        await collection.delete(ids=[str(value) for value in fact_ids])

    async def delete_end_user(self, organization_id: uuid.UUID, end_user_id: uuid.UUID) -> None:
        collection = await self._collections.open(memory_collection_for(organization_id))
        if collection is None:
            return
        await collection.delete(where=_where_equals("end_user_id", str(end_user_id)))

    async def drop(self, organization_id: uuid.UUID) -> None:
        name = memory_collection_for(organization_id)
        self._ensured.discard(name)
        await self._collections.drop(name)

    async def ids(self, organization_id: uuid.UUID) -> set[str]:
        collection = await self._collections.open(memory_collection_for(organization_id))
        if collection is None:
            return set()
        found: set[str] = set()
        offset = 0
        while True:
            page = await collection.get(limit=PAGE, offset=offset, include=[])
            ids = [str(value) for value in (page.get("ids") or [])]
            found.update(ids)
            if len(ids) < PAGE:
                return found
            offset += len(ids)

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        collection = await self._collections.open(memory_collection_for(organization_id))
        return None if collection is None else _dimension_of(collection)

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        end_user_id: uuid.UUID,
        limit: int = FACT_SEARCH_LIMIT,
        min_score: float = 0.0,
    ) -> list[Match]:
        collection = await self._collections.open(memory_collection_for(organization_id))
        if collection is None:
            return []
        found = await collection.query(
            query_embeddings=[list(vector)],
            n_results=_fetch_size(limit, filtered=min_score > 0.0),
            # Pushed down, and this is the filter that matters most in the whole module:
            # one person's durable facts reaching another person's prompt is the worst
            # outcome this system has.
            where=_where_equals("end_user_id", str(end_user_id)),
            include=["metadatas", "distances"],
        )
        matches = [match for match in _hits(found) if match.score >= min_score]
        return matches[:limit]

    async def count(
        self, organization_id: uuid.UUID, *, end_user_id: uuid.UUID | None = None
    ) -> int:
        collection = await self._collections.open(memory_collection_for(organization_id))
        if collection is None:
            return 0
        if end_user_id is None:
            return int(await collection.count())
        found = await collection.get(
            where=_where_equals("end_user_id", str(end_user_id)), include=[]
        )
        return len(list(found.get("ids") or []))


__all__ = [
    "ABSENT_KEY",
    "DIMENSION_KEY",
    "NO_EMBEDDING_FUNCTION",
    "OVERFETCH",
    "ChromaFactVectorStore",
    "ChromaVectorIndexAdmin",
    "ChromaVectorStore",
]
