"""Retrieval: the query, the filters, the deadline, and the four kinds of empty.

Everything here runs against the memory vector store and the hashing embedder — the same
second implementations ingestion is tested with — so a test that indexes a document and
then retrieves it is exercising one index rather than two fixtures that agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from app.core.metrics import build_retrieval_metrics
from app.db.models import Organization
from app.schemas.gateway_config import MemoryConfig
from app.schemas.openai import ChatMessage
from app.services.embeddings import EmbeddingError, HashEmbedder
from app.services.facts import FactRecall, FactRecaller, MemoryUnavailable
from app.services.retrieval import (
    EMPTY,
    ERROR,
    HIT,
    MAX_QUERY_CHARS,
    SKIPPED,
    TIMEOUT,
    MemoryService,
    QueryCache,
    Recall,
    Retrieval,
    RetrievalUnavailable,
    Retriever,
    build_query,
)
from app.services.vector_store import ChunkPoint, Match, MemoryVectorStore
from tests.auth_support import make_organization
from tests.end_user_support import build_end_users

ORG = uuid.UUID(int=1)
CONNECTOR = uuid.UUID(int=2)
OTHER_CONNECTOR = uuid.UUID(int=3)
DIMENSION = 64


def config(**overrides: Any) -> MemoryConfig:
    values: dict[str, Any] = {"connector_ids": [CONNECTOR]}
    values.update(overrides)
    return MemoryConfig.model_validate(values)


def turns(*pairs: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=role, content=content) for role, content in pairs]


async def indexed(
    store: MemoryVectorStore,
    embedder: HashEmbedder,
    *texts: str,
    connector: uuid.UUID = CONNECTOR,
    document: uuid.UUID | None = None,
) -> None:
    document = document or uuid.uuid4()
    await store.ensure_collection(ORG, dimension=embedder.dimension)
    vectors = await embedder.embed(list(texts))
    await store.upsert(
        ORG,
        [
            ChunkPoint(
                id=f"{document}:{index}",
                vector=vector,
                payload={
                    "org_id": str(ORG),
                    "connector_id": str(connector),
                    "document_id": str(document),
                    "source_name": "handbook.md",
                    "page_or_section": None,
                    "chunk_index": index,
                    "text": text,
                },
            )
            for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ],
    )


@pytest.fixture
def embedder() -> HashEmbedder:
    return HashEmbedder(dimension=DIMENSION, model="hash-bow")


@pytest.fixture
def vectors() -> MemoryVectorStore:
    return MemoryVectorStore()


@pytest.fixture
def retriever(embedder: HashEmbedder, vectors: MemoryVectorStore) -> Retriever:
    return Retriever(embedder, vectors)


# ---------------------------------------------------------------------------
# the query
# ---------------------------------------------------------------------------


def test_the_default_strategy_embeds_the_last_user_message() -> None:
    query = build_query(
        turns(("user", "first"), ("assistant", "answer"), ("user", "second")), config()
    )

    assert query == "second"


def test_last_n_turns_joins_the_user_turns_only() -> None:
    """The assistant's reply is longer than both questions and would dominate the vector,
    which is the opposite of carrying the subject forward."""
    query = build_query(
        turns(
            ("user", "how do refunds work"),
            ("assistant", "a very long answer " * 20),
            ("user", "and the second one?"),
        ),
        config(query_strategy="last_n_turns", query_n_turns=2),
    )

    assert query == "how do refunds work\n\nand the second one?"
    assert "very long answer" not in query


def test_a_system_message_never_reaches_the_query() -> None:
    """It is identical on every request through a gateway, so including it would pull
    every query toward the same point and return the same chunk regardless of the ask."""
    query = build_query(
        turns(("system", "You are Acme's support assistant."), ("user", "refunds?")),
        config(query_strategy="last_n_turns", query_n_turns=5),
    )

    assert query == "refunds?"


def test_a_conversation_with_no_user_turn_has_nothing_to_search_for() -> None:
    assert build_query(turns(("system", "be nice")), config()) == ""


def test_a_very_long_paste_keeps_its_tail() -> None:
    """The question is at the end of a paste, not the start."""
    query = build_query(turns(("user", "x" * 9000 + "WHERE IS THE ANSWER")), config())

    assert len(query) == MAX_QUERY_CHARS
    assert query.endswith("WHERE IS THE ANSWER")


# ---------------------------------------------------------------------------
# searching
# ---------------------------------------------------------------------------


async def test_a_relevant_chunk_is_retrieved(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    await indexed(vectors, embedder, "Refunds are issued within fourteen days of purchase.")

    result = await retriever.documents(
        organization_id=ORG, config=config(doc_min_score=0.0), messages=turns(("user", "refunds"))
    )

    assert result.outcome == HIT
    assert "fourteen days" in result.chunks[0].text
    assert result.chunks[0].source_name == "handbook.md"


async def test_no_connectors_skips_the_search_entirely(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    """Not an empty filter that matches nothing — no embedding call and no vector call,
    so a gateway without memory pays nothing for the feature."""
    await indexed(vectors, embedder, "Refunds are issued within fourteen days.")

    result = await retriever.documents(
        organization_id=ORG, config=config(connector_ids=[]), messages=turns(("user", "refunds"))
    )

    assert result.outcome == SKIPPED
    assert result.latency_ms == 0
    assert result.attempted is False


async def test_an_organization_with_no_collection_is_empty_not_an_error(
    retriever: Retriever,
) -> None:
    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert result.outcome == EMPTY
    assert result.chunks == ()


async def test_chunks_below_the_score_floor_are_dropped(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    await indexed(vectors, embedder, "Completely unrelated text about gardening tools.")

    result = await retriever.documents(
        organization_id=ORG,
        config=config(doc_min_score=0.99),
        messages=turns(("user", "refund policy")),
    )

    assert result.outcome == EMPTY
    assert result.chunks == ()


async def test_only_the_gateways_own_connectors_are_searched(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    """SPEC §5.3, one level below the tenant: a gateway reads the connectors attached to
    it and no others, even inside the same organization."""
    await indexed(vectors, embedder, "Refunds within fourteen days.", connector=CONNECTOR)
    await indexed(vectors, embedder, "Refunds within fourteen days.", connector=OTHER_CONNECTOR)

    result = await retriever.documents(
        organization_id=ORG,
        config=config(connector_ids=[OTHER_CONNECTOR], doc_min_score=0.0),
        messages=turns(("user", "refunds")),
    )

    assert {chunk.connector_id for chunk in result.chunks} == {str(OTHER_CONNECTOR)}


async def test_top_k_bounds_the_result(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    await indexed(
        vectors,
        embedder,
        *[f"refund policy paragraph {i}" for i in range(10)],
        document=uuid.uuid4(),
    )

    result = await retriever.documents(
        organization_id=ORG,
        config=config(doc_top_k=3, doc_min_score=0.0),
        messages=turns(("user", "refund policy")),
    )

    # Adjacent chunks of the same document are deduplicated, so three candidates can
    # legitimately become fewer — never more.
    assert len(result.chunks) <= 3


# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------


class StubStore:
    """A vector store that returns a fixed list, so dedup can be asserted exactly."""

    def __init__(self, matches: Sequence[Match], *, dimension: int | None = DIMENSION) -> None:
        self.matches = list(matches)
        self._dimension = dimension
        self.calls: list[dict[str, Any]] = []

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        return self._dimension

    async def search(
        self,
        organization_id: uuid.UUID,
        vector: Sequence[float],
        *,
        connector_ids: Sequence[uuid.UUID] = (),
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[Match]:
        self.calls.append(
            {"connector_ids": list(connector_ids), "limit": limit, "min_score": min_score}
        )
        return list(self.matches)


def match(score: float, *, document: str, index: int, window: int | None = None) -> Match:
    payload: dict[str, object] = {
        "document_id": document,
        "chunk_index": index,
        "text": f"chunk {index}",
        "source_name": "handbook.md",
        "connector_id": str(CONNECTOR),
    }
    if window is not None:
        # What task 20's `sentence_window` writes: the sentence that was embedded, and how
        # far the chunk's text reaches either side of it.
        payload["window_sentences"] = window
        payload["embedded_text"] = f"sentence {index}"
    return Match(id=f"{document}:{index}", score=score, payload=payload)


async def test_an_adjacent_chunk_of_the_same_document_is_dropped(
    embedder: HashEmbedder,
) -> None:
    """Chunks overlap by design, so neighbours share a sixth of their text and score
    alike. Two of them spends the budget twice on one passage."""
    store = StubStore(
        [
            match(0.90, document="a", index=4),
            match(0.88, document="a", index=5),
            match(0.70, document="b", index=0),
        ]
    )
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert [(chunk.document_id, chunk.chunk_index) for chunk in result.chunks] == [
        ("a", 4),
        ("b", 0),
    ]


async def test_a_distant_chunk_of_the_same_document_is_kept(embedder: HashEmbedder) -> None:
    store = StubStore([match(0.90, document="a", index=1), match(0.80, document="a", index=9)])
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert len(result.chunks) == 2


async def test_the_higher_scoring_of_a_neighbouring_pair_survives(
    embedder: HashEmbedder,
) -> None:
    store = StubStore([match(0.90, document="a", index=5), match(0.60, document="a", index=4)])
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert [chunk.score for chunk in result.chunks] == [0.90]


async def test_a_windowed_chunk_drops_neighbours_as_far_as_its_window_reaches(
    embedder: HashEmbedder,
) -> None:
    """How far "neighbouring" reaches is a property of the chunk, not a constant. With a
    window of two, five consecutive chunks contain the same sentence, and the default
    radius of one would inject that paragraph three times over."""
    store = StubStore(
        [
            match(0.90, document="a", index=10, window=2),
            match(0.88, document="a", index=12, window=2),
            match(0.70, document="a", index=20, window=2),
        ]
    )
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert [chunk.chunk_index for chunk in result.chunks] == [10, 20]


async def test_a_windowed_chunk_reports_the_sentence_that_matched(
    embedder: HashEmbedder,
) -> None:
    """Two strings with two different jobs: the window is what goes into the prompt, the
    sentence is what the query actually matched."""
    store = StubStore([match(0.90, document="a", index=3, window=1)])
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert result.chunks[0].matched_text == "sentence 3"
    assert result.chunks[0].text == "chunk 3"


async def test_an_ordinary_chunk_has_no_matched_text_to_report(
    embedder: HashEmbedder,
) -> None:
    """``None`` rather than a copy of the text, so the distinction does not leak into every
    screen: a chunk whose matched text is its own text has nothing to say about it."""
    store = StubStore([match(0.90, document="a", index=3)])
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert result.chunks[0].matched_text is None


async def test_the_score_floor_and_the_connector_filter_are_pushed_into_the_store(
    embedder: HashEmbedder,
) -> None:
    """Filtering afterwards would make `doc_top_k` mean "this many candidates" rather
    than "this many usable chunks"."""
    store = StubStore([])
    retriever = Retriever(embedder, store)  # type: ignore[arg-type]

    await retriever.documents(
        organization_id=ORG,
        config=config(doc_top_k=4, doc_min_score=0.42),
        messages=turns(("user", "refunds")),
    )

    assert store.calls == [
        {"connector_ids": [CONNECTOR], "limit": 4, "min_score": 0.42},
    ]


# ---------------------------------------------------------------------------
# failure
# ---------------------------------------------------------------------------


class SlowStore(StubStore):
    def __init__(self, delay: float) -> None:
        super().__init__([])
        self._delay = delay

    async def search(self, *args: Any, **kwargs: Any) -> list[Match]:
        await asyncio.sleep(self._delay)
        return []


class BrokenStore(StubStore):
    async def search(self, *args: Any, **kwargs: Any) -> list[Match]:
        raise RuntimeError("qdrant is restarting")


class BrokenEmbedder:
    model = "hash-bow"
    dimension = DIMENSION

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("the provider is rate limiting", retryable=True)


async def test_a_slow_search_times_out_at_the_configured_deadline(
    embedder: HashEmbedder,
) -> None:
    retriever = Retriever(embedder, SlowStore(1.0))  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG,
        config=config(retrieval_timeout_ms=50),
        messages=turns(("user", "refunds")),
    )

    assert result.outcome == TIMEOUT
    assert "50 ms" in (result.error or "")


async def test_a_broken_vector_store_is_an_error_not_an_exception(
    embedder: HashEmbedder,
) -> None:
    retriever = Retriever(embedder, BrokenStore([]))  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert result.outcome == ERROR
    # The provider's own words never reach the caller; they go to the structured log.
    assert "qdrant" not in (result.error or "").lower()


async def test_a_broken_embedding_provider_is_the_same_kind_of_failure() -> None:
    """From this request's point of view they are one event: memory is unavailable, and
    `on_retrieval_error` decides what that means."""
    retriever = Retriever(BrokenEmbedder(), StubStore([]))  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert result.outcome == ERROR


async def test_an_index_built_by_a_different_model_is_refused_rather_than_searched(
    embedder: HashEmbedder,
) -> None:
    """Otherwise every request quietly loses its documents and nothing says why."""
    retriever = Retriever(embedder, StubStore([], dimension=1536))  # type: ignore[arg-type]

    result = await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )

    assert result.outcome == ERROR
    assert "1536" in (result.error or "")
    assert str(DIMENSION) in (result.error or "")


def test_fail_open_proceeds_without_memory() -> None:
    Retrieval(outcome=TIMEOUT, error="too slow").enforce("fail_open")


def test_fail_closed_refuses_the_request() -> None:
    with pytest.raises(RetrievalUnavailable) as caught:
        Retrieval(outcome=TIMEOUT, error="too slow").enforce("fail_closed")

    assert caught.value.status_code == 503
    assert "timed out" in caught.value.message


def test_fail_closed_does_not_refuse_a_merely_empty_result() -> None:
    """Finding nothing is a normal outcome, not a failure. Refusing it would make a
    gateway 503 for every question its documents happen not to cover."""
    Retrieval(outcome=EMPTY).enforce("fail_closed")


# ---------------------------------------------------------------------------
# the embedding cache
# ---------------------------------------------------------------------------


class CountingEmbedder:
    model = "hash-bow"
    dimension = DIMENSION

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[0.0] * DIMENSION for _ in texts]


async def test_an_identical_query_is_embedded_once() -> None:
    counting = CountingEmbedder()
    retriever = Retriever(counting, StubStore([]))  # type: ignore[arg-type]

    for _ in range(3):
        await retriever.documents(
            organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
        )

    assert counting.calls == 1


async def test_a_different_query_is_embedded_again() -> None:
    counting = CountingEmbedder()
    retriever = Retriever(counting, StubStore([]))  # type: ignore[arg-type]

    await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )
    await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "shipping"))
    )

    assert counting.calls == 2


def test_the_cache_expires() -> None:
    cache = QueryCache(ttl_seconds=0.0)
    cache.put("m", "q", [1.0])

    assert cache.get("m", "q") is None


def test_the_cache_is_keyed_by_model_as_well_as_text() -> None:
    """A model change must not serve vectors from the old one — they are not comparable
    and the index would be searched with a stranger's coordinates."""
    cache = QueryCache()
    cache.put("model-a", "q", [1.0])

    assert cache.get("model-b", "q") is None


def test_the_cache_is_bounded() -> None:
    cache = QueryCache(max_entries=2)
    cache.put("m", "one", [1.0])
    cache.put("m", "two", [1.0])
    cache.put("m", "three", [1.0])

    assert cache.get("m", "one") is None
    assert cache.get("m", "three") is not None


# ---------------------------------------------------------------------------
# metrics and the fan-out
# ---------------------------------------------------------------------------


def counter(registry: CollectorRegistry, outcome: str) -> float:
    value = registry.get_sample_value("retrieval_attempts_total", {"outcome": outcome})
    return value or 0.0


async def test_every_outcome_is_counted_by_name(embedder: HashEmbedder) -> None:
    registry = CollectorRegistry()
    metrics = build_retrieval_metrics(registry)
    retriever = Retriever(embedder, StubStore([]), metrics=metrics)  # type: ignore[arg-type]

    await retriever.documents(
        organization_id=ORG, config=config(), messages=turns(("user", "refunds"))
    )
    await retriever.documents(
        organization_id=ORG, config=config(connector_ids=[]), messages=turns(("user", "x"))
    )

    assert counter(registry, EMPTY) == 1.0
    assert counter(registry, SKIPPED) == 1.0


async def test_a_skipped_request_is_not_timed(embedder: HashEmbedder) -> None:
    """It would report a zero nobody waited, and drag the p95 toward it."""
    registry = CollectorRegistry()
    metrics = build_retrieval_metrics(registry)
    retriever = Retriever(embedder, StubStore([]), metrics=metrics)  # type: ignore[arg-type]

    await retriever.documents(
        organization_id=ORG, config=config(connector_ids=[]), messages=turns(("user", "x"))
    )

    assert registry.get_sample_value("retrieval_duration_seconds_count") == 0.0


async def test_recall_runs_both_branches_and_returns_both(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    """The fan-out SPEC §6.3 asks for. Documents from the index, facts from the store,
    and one ``Recall`` carrying both outcomes separately."""
    await indexed(vectors, embedder, "Refunds within fourteen days.")
    memory = build_end_users(organization_in(ORG), embedder=embedder)
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Works in the EU and needs GDPR-compliant answers.")
    service = MemoryService(retriever, recaller=memory.recaller)

    recall = await service.recall(
        organization_id=ORG,
        config=config(doc_min_score=0.0),
        messages=turns(("user", "refunds")),
        end_user_id=alice.id,
    )

    assert recall.documents.outcome == HIT
    assert [fact.text for fact in recall.facts] == [
        "Works in the EU and needs GDPR-compliant answers."
    ]
    assert recall.end_user_id == alice.id


async def test_a_gateway_with_no_conversation_memory_still_retrieves_documents(
    retriever: Retriever, vectors: MemoryVectorStore, embedder: HashEmbedder
) -> None:
    """``recaller=None`` is what the editor's preview and most of this suite build."""
    await indexed(vectors, embedder, "Refunds within fourteen days.")

    recall = await MemoryService(retriever).recall(
        organization_id=ORG,
        config=config(doc_min_score=0.0),
        messages=turns(("user", "refunds")),
    )

    assert recall.documents.outcome == HIT
    assert recall.facts == ()


async def test_the_two_halves_run_concurrently_rather_than_one_after_the_other(
    embedder: HashEmbedder,
) -> None:
    """The acceptance criterion, measured. Both stores sleep for the same interval; if
    the branches were sequential the wall clock would be two of them.

    The assertion is on the *measured* elapsed time rather than on call ordering, because
    ordering can be right while an ``await`` in the wrong place still serialises them.
    """
    delay = 0.15
    memory = build_end_users(make_organization(), embedder=embedder)
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Prefers Python.")

    slow_documents = SlowStore(delay)
    service = MemoryService(
        Retriever(embedder, slow_documents),  # type: ignore[arg-type]
        recaller=FactRecaller(embedder, SlowFacts(memory.vectors, delay), memory.store),  # type: ignore[arg-type]
    )

    started = time.perf_counter()
    recall = await service.recall(
        organization_id=memory.organization_id,
        config=config(doc_min_score=0.0, memory_min_score=0.0, retrieval_timeout_ms=5000),
        messages=turns(("user", "python")),
        end_user_id=alice.id,
    )
    elapsed = time.perf_counter() - started

    assert recall.documents.attempted and recall.memory.attempted
    # Comfortably under the sum, comfortably over one delay: a serial pair would be at
    # least `2 * delay`, and the margins are wide enough not to measure this machine.
    assert delay <= elapsed < delay * 1.8


async def test_the_question_is_embedded_once_for_both_halves(
    embedder: HashEmbedder,
) -> None:
    """Two provider calls for one question is a bill nobody agreed to. The two branches
    share an in-flight task through the query cache."""
    counting = WrappedEmbedder(embedder)
    memory = build_end_users(make_organization(), embedder=embedder)
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Prefers Python.")

    service = MemoryService(
        Retriever(counting, StubStore([]), cache=QueryCache()),  # type: ignore[arg-type]
        recaller=FactRecaller(counting, memory.vectors, memory.store),
    )
    await service.recall(
        organization_id=memory.organization_id,
        config=config(doc_min_score=0.0, memory_min_score=0.0),
        messages=turns(("user", "python")),
        end_user_id=alice.id,
    )

    assert counting.calls == 1


async def test_a_cancelled_awaiter_does_not_cancel_the_shared_embedding(
    embedder: HashEmbedder,
) -> None:
    """The shield in :meth:`QueryCache.embed`.

    Both halves of memory await the same in-flight embedding under *independent*
    timeouts. Without the shield, whichever branch gave up first would cancel the shared
    task and take the other one down with it — turning one branch's timeout into both
    branches failing, which is exactly what running them concurrently was meant to
    prevent.
    """
    cache = QueryCache()
    slow = SlowEmbedder(embedder, 0.1)

    first = asyncio.create_task(cache.embed(slow, "refunds"))
    second = asyncio.create_task(cache.embed(slow, "refunds"))
    await asyncio.sleep(0.01)
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first

    vector = await second
    assert len(vector) == DIMENSION
    assert slow.calls == 1


async def test_enforce_names_the_document_half_when_both_failed() -> None:
    """The index is the half an operator can act on, and the half a fail-closed gateway
    was almost certainly configured for."""
    recall = Recall(
        documents=Retrieval(outcome=TIMEOUT, error="docs"),
        memory=FactRecall(outcome=TIMEOUT, error="facts"),
    )

    with pytest.raises(RetrievalUnavailable):
        recall.enforce("fail_closed")


async def test_enforce_still_refuses_when_only_conversation_memory_failed() -> None:
    recall = Recall(memory=FactRecall(outcome=ERROR, error="facts"))

    with pytest.raises(MemoryUnavailable):
        recall.enforce("fail_closed")


async def test_latency_is_the_slower_branch_rather_than_the_sum() -> None:
    """They run concurrently, so adding them would report a number nobody waited for —
    and would make the SPEC §10.3 waterfall stop subtracting."""
    recall = Recall(
        documents=Retrieval(outcome=HIT, latency_ms=40),
        memory=FactRecall(outcome=HIT, latency_ms=70),
    )

    assert recall.latency_ms == 70


async def test_injected_tokens_are_observed_once_per_request(embedder: HashEmbedder) -> None:
    registry = CollectorRegistry()
    metrics = build_retrieval_metrics(registry)
    service = MemoryService(Retriever(embedder, StubStore([])), metrics=metrics)  # type: ignore[arg-type]

    service.injected(412)

    assert registry.get_sample_value("retrieval_injected_tokens_count") == 1.0
    assert registry.get_sample_value("retrieval_injected_tokens_sum") == 412.0


# ---------------------------------------------------------------------------
# doubles for the concurrency assertions
# ---------------------------------------------------------------------------


class SlowFacts:
    """The real fact index behind a fixed delay, so the result is still real."""

    def __init__(self, inner: Any, delay: float) -> None:
        self.inner = inner
        self.delay = delay

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        size: int | None = await self.inner.dimension(organization_id)
        return size

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(self.delay)
        return await self.inner.search(*args, **kwargs)


class WrappedEmbedder:
    """The real embedder, counted. Distinct from :class:`CountingEmbedder` above, which
    returns zero vectors — these tests need results that actually rank."""

    def __init__(self, inner: HashEmbedder) -> None:
        self.inner = inner
        self.calls = 0

    @property
    def model(self) -> str:
        return self.inner.model

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return await self.inner.embed(list(texts))


class SlowEmbedder(WrappedEmbedder):
    def __init__(self, inner: HashEmbedder, delay: float) -> None:
        super().__init__(inner)
        self.delay = delay

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        await asyncio.sleep(self.delay)
        return await super().embed(texts)


def organization_in(organization_id: uuid.UUID) -> Organization:
    """An organization with a chosen id, so one ``Recall`` covers one tenant.

    The two halves of memory are two collections keyed by the same organization; a
    fixture that invented its own id would produce a test where the document half and the
    fact half are about different customers, and pass anyway.
    """
    organization = make_organization()
    organization.id = organization_id
    return organization
