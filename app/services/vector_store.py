"""The document index, behind a port.

**A port with no vendor in it.** Since task 19 there are two real backends —
:mod:`app.services.vector_qdrant` and :mod:`app.services.vector_chroma` — and this module
imports neither. What is left here is the vocabulary they both have to satisfy: the
collection naming, the deterministic point ids, the protocol, and a brute-force
implementation for tests.

One collection per organization, ``org_{org_id}_docs`` (SPEC §9.4). A collection per
tenant rather than one collection with an ``org_id`` filter, and the reason is the same
one that makes SPEC §5.3 a hard rule: a filter that is *forgotten* returns another
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
``tests/vector_store_contract.py`` runs one set of assertions against all three. The two
server-backed runs need real servers — payload filters, delete-by-filter and the meaning
of ``limit`` under a score floor are precisely where a hand-written double agrees with
itself and disagrees with everything else.
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

    async def delete_points(self, organization_id: uuid.UUID, ids: Sequence[str]) -> None:
        """Named points, by id — the one delete here that is *not* by filter, for the one
        point whose id is known by construction: a document's summary chunk (task 102),
        written under a constant index. Ids that do not exist are not an error."""
        ...

    async def drop(self, organization_id: uuid.UUID) -> None:
        """Remove the whole collection. Offboarding, and nothing else."""
        ...

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        """The vector width this tenant's collection was built with, or ``None`` when
        there is no collection yet.

        Retrieval asks so it can refuse to search an index built by a different embedding
        model. Without it a changed ``EMBEDDING_DIMENSION`` is a silent failure: a backend
        rejects the mismatched query vector with some message about dimensions, which
        under ``fail_open`` becomes "the model answered without its documents" on every
        request and nothing anywhere says why. A backend that infers its width from the
        first insert has to record it somewhere it can read back, because answering
        ``None`` here when a collection exists is the same silent failure.
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
    ) -> list[Match]:
        """The nearest chunks, best first.

        Two things a backend must mean by this, both pinned by the contract suite because
        the natural implementation of each differs per vendor:

        ``score`` is **cosine similarity in [-1, 1], higher is better** — not a distance.
        A backend that returns distances converts here. Getting that backwards does not
        raise; it returns the worst matches, confidently ranked, and the only symptom is
        that answers get vaguer.

        ``limit`` bounds the results *after* ``min_score``, so it means "this many chunks
        above the floor" rather than "this many candidates, some of which are discarded".
        A backend that cannot push a threshold down has to over-fetch to honour it.
        """
        ...

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
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryVectorStore:
    """Brute-force cosine over a dict. Exact, which is what a test wants.

    It models the *indirection* a reindex needs, without modelling anybody's version of
    it: ``collections`` is keyed by the physical name and ``live_collections`` maps the
    name callers use onto whichever one is live. Task 17 wrote this as an emulation of
    Qdrant's aliases, which was one vendor's mechanism leaking into the double; the
    behaviour is unchanged and the name now says what the port says. Without the
    indirection at all, a reindex would pass here — two dictionary keys, swapped — and
    fail against every real backend, where the swap is the only part that is hard.
    """

    collections: dict[str, dict[str, ChunkPoint]] = field(default_factory=dict)
    dimensions: dict[str, int] = field(default_factory=dict)
    #: Logical name to the physical collection currently serving it. Populated by
    #: ``ensure_collection`` and by a promotion.
    live_collections: dict[str, str] = field(default_factory=dict)

    def live(self, organization_id: uuid.UUID) -> str:
        """The collection this tenant's reads resolve to right now.

        Falls back to the logical name itself, which is both the pre-indirection shape
        and what makes a store somebody filled in by hand behave the way it always did.
        """
        logical = collection_for(organization_id)
        return self.live_collections.get(logical, logical)

    async def ensure_collection(self, organization_id: uuid.UUID, *, dimension: int) -> None:
        logical = collection_for(organization_id)
        name = self.live_collections.get(logical)
        if name is None:
            name = logical if logical in self.collections else f"{logical}_v1"
            self.live_collections[logical] = name
        self.collections.setdefault(name, {})
        self.dimensions[name] = dimension

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[ChunkPoint]) -> None:
        name = self.live(organization_id)
        if name not in self.collections:
            raise KeyError(f"collection {name} does not exist")
        expected = self.dimensions[name]
        for point in points:
            if len(point.vector) != expected:
                # Every real backend refuses this, one way or another. Accepting it
                # here would let a dimension bug pass every test and fail only in
                # production.
                raise ValueError(
                    f"vector has {len(point.vector)} dimensions, collection expects {expected}"
                )
            self.collections[name][point.id] = point

    async def delete_document(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self._delete_where(organization_id, "document_id", str(document_id))

    async def delete_connector(self, organization_id: uuid.UUID, connector_id: uuid.UUID) -> None:
        self._delete_where(organization_id, "connector_id", str(connector_id))

    async def delete_points(self, organization_id: uuid.UUID, ids: Sequence[str]) -> None:
        points = self.collections.get(self.live(organization_id))
        if points is None:
            return
        for identifier in ids:
            points.pop(identifier, None)

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
        self.live_collections.pop(collection_for(organization_id), None)

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
    "Stored",
    "VectorStore",
    "collection_for",
    "cosine",
    "point_id",
]
