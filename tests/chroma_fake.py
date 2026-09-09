"""A stand-in that speaks Chroma's dialect, for testing the translation into it.

**What this proves, and what it does not.** The whole argument of
``tests/vector_store_contract.py`` is that a hand-written double agrees with itself, so
running the contract against one proves nothing about a server. That argument still holds
and this fake does not dodge it. What it is for is narrower and real: everything in
:mod:`app.services.vector_chroma` that is a *translation* rather than a query — cosine
distance into similarity, a score floor into an over-fetch, ``where`` operators, a payload
split across ``documents`` and ``metadatas``, null fields, offset paging — is logic in this
repository, and logic in this repository should have tests that run everywhere.

So the fake is deliberately built to be **awkward in the ways Chroma is awkward**:

* it answers with distances, not scores, so a module that forgot to convert fails here;
* it returns parallel lists and omits the ones that were not ``include``d, so an
  implementation that indexes them blindly fails here;
* ``n_results`` counts results *after* the ``where`` filter, so it cannot hide a
  filter applied client-side;
* it refuses a vector of the wrong width, the way a real server does;
* it raises the client library's **own** ``NotFoundError`` for a missing collection
  rather than returning empty, so the module's not-found handling is exercised as it
  will actually run and not against a shape invented here.

It is *not* built to be a plausible vector database. Search is brute force, ordering is
exact, and nothing here is approximate — which is the opposite of what an HNSW index does
and exactly why the real run in ``tests/test_vector_chroma.py`` is marked ``chroma`` and
needs a server.

Every call's keyword arguments are recorded on :attr:`FakeChromaClient.calls`, which is how
the ``embedding_function`` assertion is made: not "the module intends to pass None" but
"None reached the client, on every call that opens or creates a collection".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from chromadb.errors import NotFoundError


@dataclass(frozen=True, slots=True)
class Recorded:
    method: str
    kwargs: dict[str, Any]


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 1.0
    return 1.0 - dot / (left_norm * right_norm)


def _matches(metadata: dict[str, Any], where: dict[str, Any] | None) -> bool:
    """The subset of Chroma's ``where`` grammar this system actually issues."""
    for key, condition in (where or {}).items():
        value = metadata.get(key)
        if not isinstance(condition, dict):
            if value != condition:
                return False
            continue
        for operator, operand in condition.items():
            if operator == "$eq" and value != operand:
                return False
            if operator == "$in" and value not in operand:
                return False
            if operator not in {"$eq", "$in"}:
                raise AssertionError(f"the fake does not implement {operator}")
    return True


@dataclass
class FakeChromaCollection:
    name: str
    metadata: dict[str, Any]
    calls: list[Recorded]
    ids: list[str] = field(default_factory=list)
    embeddings: dict[str, list[float]] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    metadatas: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def upsert(
        self,
        *,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str] | None = None,
        metadatas: list[dict[str, Any]] | None = None,
    ) -> None:
        expected = self.metadata.get("gateway_dimension")
        for position, identifier in enumerate(ids):
            vector = list(embeddings[position])
            if expected is not None and len(vector) != int(expected):
                # A real server refuses this. A permissive double would let a dimension
                # bug pass every test and fail only in production.
                raise ValueError(
                    f"Embedding dimension {len(vector)} does not match collection "
                    f"dimensionality {expected}"
                )
            if identifier not in self.embeddings:
                self.ids.append(identifier)
            self.embeddings[identifier] = vector
            if documents is not None:
                self.documents[identifier] = documents[position]
            if metadatas is not None:
                self.metadatas[identifier] = dict(metadatas[position])

    def _selected(self, where: dict[str, Any] | None, ids: list[str] | None) -> list[str]:
        wanted = set(ids) if ids is not None else None
        return [
            identifier
            for identifier in self.ids
            if (wanted is None or identifier in wanted)
            and _matches(self.metadatas.get(identifier, {}), where)
        ]

    async def get(
        self,
        *,
        ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(Recorded("get", {"where": where, "limit": limit, "offset": offset}))
        included = list(include or ["metadatas", "documents"])
        selected = self._selected(where, ids)
        start = offset or 0
        window = selected[start : start + limit] if limit is not None else selected[start:]
        result: dict[str, Any] = {"ids": window, "included": included}
        # Omitted when not included, which is what the real result does and what an
        # implementation that indexes the lists blindly gets wrong.
        if "documents" in included:
            result["documents"] = [self.documents.get(key, "") for key in window]
        if "metadatas" in included:
            result["metadatas"] = [dict(self.metadatas.get(key, {})) for key in window]
        if "embeddings" in included:
            result["embeddings"] = [list(self.embeddings[key]) for key in window]
        return result

    async def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int = 10,
        where: dict[str, Any] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(Recorded("query", {"n_results": n_results, "where": where}))
        included = list(include or ["metadatas", "documents", "distances"])
        vector = list(query_embeddings[0])
        # The filter narrows before `n_results` counts, exactly as the server does. A
        # store that filtered afterwards would silently return fewer results than asked.
        ranked = sorted(
            self._selected(where, None),
            key=lambda key: (_cosine_distance(vector, self.embeddings[key]), key),
        )[:n_results]
        result: dict[str, Any] = {"ids": [ranked], "included": included}
        if "distances" in included:
            result["distances"] = [
                [_cosine_distance(vector, self.embeddings[key]) for key in ranked]
            ]
        if "documents" in included:
            result["documents"] = [[self.documents.get(key, "") for key in ranked]]
        if "metadatas" in included:
            result["metadatas"] = [[dict(self.metadatas.get(key, {})) for key in ranked]]
        return result

    async def delete(
        self, *, ids: list[str] | None = None, where: dict[str, Any] | None = None
    ) -> None:
        for identifier in self._selected(where, ids):
            self.ids.remove(identifier)
            self.embeddings.pop(identifier, None)
            self.documents.pop(identifier, None)
            self.metadatas.pop(identifier, None)

    async def count(self) -> int:
        return len(self.ids)


@dataclass
class FakeChromaClient:
    collections: dict[str, FakeChromaCollection] = field(default_factory=dict)
    calls: list[Recorded] = field(default_factory=list)

    async def get_or_create_collection(
        self,
        *,
        name: str,
        configuration: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        embedding_function: Any = "unset",
    ) -> FakeChromaCollection:
        self.calls.append(
            Recorded(
                "get_or_create_collection",
                {
                    "name": name,
                    "configuration": configuration,
                    "metadata": metadata,
                    "embedding_function": embedding_function,
                },
            )
        )
        existing = self.collections.get(name)
        if existing is not None:
            return existing
        created = FakeChromaCollection(name=name, metadata=dict(metadata or {}), calls=self.calls)
        self.collections[name] = created
        return created

    async def get_collection(
        self, *, name: str, embedding_function: Any = "unset"
    ) -> FakeChromaCollection:
        self.calls.append(
            Recorded("get_collection", {"name": name, "embedding_function": embedding_function})
        )
        found = self.collections.get(name)
        if found is None:
            raise NotFoundError(f"Collection {name} does not exist")
        return found

    async def delete_collection(self, *, name: str) -> None:
        self.calls.append(Recorded("delete_collection", {"name": name}))
        if name not in self.collections:
            raise NotFoundError(f"Collection {name} does not exist")
        del self.collections[name]

    async def list_collections(self) -> list[FakeChromaCollection]:
        return list(self.collections.values())

    def opened_with(self, method: str) -> list[Any]:
        """Every ``embedding_function`` this client was called with, for one method."""
        return [
            call.kwargs.get("embedding_function", "unset")
            for call in self.calls
            if call.method == method
        ]
