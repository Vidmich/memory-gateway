"""Finding the documents that belong in a prompt (SPEC §6.3).

Retrieval is the half of the memory promise that runs while a customer is waiting, and
that single fact decides almost everything here.

**It is bounded, and the bound is per gateway.** Every call runs inside
``retrieval_timeout_ms``. SPEC §4.2 budgets the whole gateway 150 ms at p95 and the
default timeout is 800 ms, which looks contradictory until you notice they measure
different things: 150 ms is what retrieval should cost, 800 ms is when we stop believing
it will finish. A timeout is not a failure of the request — it is a decision the gateway's
owner already made, in ``on_retrieval_error``.

**Failure is a policy, not an exception.** :meth:`Retriever.documents` never raises for a
retrieval failure. It returns a :class:`Retrieval` carrying an outcome, and the *caller*
applies the gateway's policy through :meth:`Retrieval.enforce`. That split is what lets
the same code serve a live request under ``fail_open`` — proceed, ungrounded — and answer
the Try-retrieval box in the editor with a readable error instead of a 503.

**Nothing is searched when there is nothing to search.** An empty ``connector_ids`` skips
the embedding *and* the vector call, and so does an organization with no collection yet.
Both are common — a gateway before anyone attaches a connector, and one attached to a
connector still ingesting — and neither should cost a provider round trip.

**A dimension mismatch is shouted about rather than absorbed.** If the collection was
built by a different embedding model than the one configured now, the query vector is the
wrong width. Qdrant rejects it; the memory store would happily score it as zero. Either
way, under ``fail_open``, every request quietly loses its documents and nothing says why.
So the width is checked before the search and logged at ``error``, which is the one
severity that is hard to miss.

The one thing deliberately *not* here is query rewriting. The query is the last user
message (or the last N user turns), which fails on a conversational follow-up — "what
about the second one?" retrieves nothing useful. SPEC §17.4 leaves that open, and the way
to close it with evidence rather than intuition is the empty-retrieval rate this module
counts on every request.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.api.proxy.errors import GatewayUnavailable
from app.core.metrics import RetrievalMetrics
from app.schemas.gateway_config import MemoryConfig
from app.schemas.openai import ChatMessage
from app.services.embeddings import Embedder
from app.services.prompt import as_text
from app.services.vector_store import Match, VectorStore

logger = logging.getLogger(__name__)

#: How long an identical query's embedding is reused. Seconds, not minutes: the win is a
#: retry, a refresh of the Try-retrieval box, or an evaluation run firing the same
#: question at two gateways — all of which happen within a few seconds. Anything longer
#: starts caching across a redeployment of the embedding model for no extra benefit.
QUERY_CACHE_TTL_SECONDS = 30.0
QUERY_CACHE_MAX_ENTRIES = 512

#: Longest query text embedded. A conversation's last user turn can be a pasted file;
#: embedding all of it costs money and produces a vector that is about everything and
#: therefore about nothing. The tail is kept rather than the head — the question is
#: usually at the end of a long paste.
MAX_QUERY_CHARS = 4000

#: Outcomes, and the label on the ``outcome`` metric. ``skipped`` means retrieval was not
#: attempted (no connectors, memory switched off for this request); ``empty`` means it ran
#: and found nothing above the score floor, which is the signal the monitoring screen
#: watches; ``error`` and ``timeout`` are the two failures ``on_retrieval_error`` governs.
SKIPPED = "skipped"
HIT = "hit"
EMPTY = "empty"
TIMEOUT = "timeout"
ERROR = "error"

FAILED_OUTCOMES = frozenset({TIMEOUT, ERROR})


class RetrievalUnavailable(GatewayUnavailable):
    """``on_retrieval_error = fail_closed`` and retrieval did not answer.

    A 503 with the same shape as any other gateway failure, so a client SDK treats it as
    retryable — which it is, unlike a 400. The message names the policy, because a caller
    seeing a 503 from a gateway whose upstream is perfectly healthy needs to know this is
    a configured refusal rather than a fault.
    """


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrieved excerpt, flattened out of the vector payload.

    A dataclass rather than the raw :class:`~app.services.vector_store.Match` because
    three different callers read it — the assembler, the request log, and the editor's
    Try-retrieval box — and each of them reaching into a dictionary with string keys is
    three chances to misspell ``page_or_section``.
    """

    id: str
    score: float
    text: str
    source_name: str
    page_or_section: str | None
    document_id: str | None
    connector_id: str | None
    chunk_index: int

    @classmethod
    def of(cls, match: Match) -> Chunk:
        payload = match.payload
        section = payload.get("page_or_section")
        return cls(
            id=match.id,
            score=match.score,
            text=str(payload.get("text", "")),
            # A document whose name is missing is still a usable citation target by id;
            # rendering "source: None" into somebody's prompt is not.
            source_name=str(payload.get("source_name") or "untitled"),
            page_or_section=str(section) if section else None,
            document_id=_as_str(payload.get("document_id")),
            connector_id=_as_str(payload.get("connector_id")),
            chunk_index=int(payload.get("chunk_index", 0) or 0),
        )

    def as_log_entry(self, *, injected: bool, dropped_reason: str | None = None) -> dict[str, Any]:
        """The row that goes into ``request_logs.retrieved_chunk_ids``.

        A record rather than a bare id, and that is the whole design: the drawer has to
        show "handbook.md (p. 12), 0.71, dropped — over the token budget" for a request
        that happened last week, and the alternative is joining a chunk id against a
        vector store that has since been reindexed. The chunk this request actually used
        may no longer exist; what it *was* is the thing worth keeping.
        """
        entry: dict[str, Any] = {
            "id": self.id,
            "score": round(self.score, 6),
            "document_id": self.document_id,
            "source_name": self.source_name,
            "page_or_section": self.page_or_section,
            "chunk_index": self.chunk_index,
            "injected": injected,
        }
        if dropped_reason is not None:
            entry["dropped"] = dropped_reason
        return entry


@dataclass(frozen=True, slots=True)
class Retrieval:
    """What one retrieval attempt produced, including the attempts that produced nothing.

    ``chunks`` is empty for four different reasons — not attempted, nothing indexed,
    nothing above the floor, and something broke — and they are not the same reason.
    ``outcome`` is what tells them apart, on the metric, in the log, and in the editor.
    """

    chunks: tuple[Chunk, ...] = ()
    outcome: str = SKIPPED
    latency_ms: int = 0
    #: A sentence for a human, present only on a failure. Never the provider's raw
    #: exception text — that goes to the structured log, which is not customer-facing.
    error: str | None = None
    #: The text that was actually embedded, so the editor can show what a conversational
    #: follow-up turned into. Usually the surprise that explains a bad result.
    query: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome in FAILED_OUTCOMES

    @property
    def attempted(self) -> bool:
        """Whether a search actually ran. ``skipped`` requests have no retrieval latency
        to record and must not count toward the empty-retrieval rate — a gateway with no
        connectors is not a gateway retrieving nothing, it is one not retrieving."""
        return self.outcome != SKIPPED

    def enforce(self, policy: str) -> None:
        """Apply ``on_retrieval_error``. Raises only under ``fail_closed``.

        Called by the request path rather than by :class:`Retriever`, so that the same
        retrieval a live request would perform can be rendered in the editor without a
        gateway's availability policy turning a diagnostic into a 503.
        """
        if self.failed and policy == "fail_closed":
            raise RetrievalUnavailable(
                f"This gateway is configured to refuse requests it cannot ground in the "
                f"organization's documents, and retrieval {self._verb()}. "
                f"{self.error or ''}".strip()
            )

    def _verb(self) -> str:
        return "timed out" if self.outcome == TIMEOUT else "failed"


NOTHING = Retrieval()


# ---------------------------------------------------------------------------
# the query
# ---------------------------------------------------------------------------


def build_query(messages: Sequence[ChatMessage], config: MemoryConfig) -> str:
    """The text to embed, per ``query_strategy`` (SPEC §6.3).

    System messages are excluded from both strategies, and that is not a detail: a
    gateway's system context is the same on every request, so including it would pull
    every query toward the same point in the embedding space and make the top result the
    same chunk regardless of what was asked.

    Assistant turns are excluded too. ``last_n_turns`` concatenates the last N *user*
    turns because the point of the strategy is to carry the subject of a conversation
    forward — "and the second one?" needs the question before it, not the model's own
    previous answer, which is longer than both and would dominate the vector.
    """
    user_turns = [
        text
        for message in messages
        if message.role == "user" and (text := as_text(message.content).strip())
    ]
    if not user_turns:
        return ""

    if config.query_strategy == "last_n_turns":
        chosen = user_turns[-config.query_n_turns :]
    else:
        chosen = user_turns[-1:]

    # The tail, not the head: in a long paste the actual question is at the end.
    return "\n\n".join(chosen)[-MAX_QUERY_CHARS:]


# ---------------------------------------------------------------------------
# the embedding cache
# ---------------------------------------------------------------------------


@dataclass
class QueryCache:
    """A tiny TTL map from query text to its embedding.

    Not a general cache and deliberately not Redis. The value is a few kilobytes of
    floats, the hit window is seconds, and a network round trip to fetch one would cost
    about what recomputing it costs. Per-process is exactly the right scope: the repeated
    query almost always comes from the same person pressing the same button.

    Eviction is oldest-first on insertion order rather than least-recently-used. The map
    holds hundreds of entries with a thirty-second life; the difference between the two
    policies at that size is not measurable, and one of them is four lines.
    """

    ttl_seconds: float = QUERY_CACHE_TTL_SECONDS
    max_entries: int = QUERY_CACHE_MAX_ENTRIES
    _entries: dict[tuple[str, str], tuple[float, list[float]]] = field(default_factory=dict)

    def get(self, model: str, text: str) -> list[float] | None:
        key = (model, text)
        found = self._entries.get(key)
        if found is None:
            return None
        stored_at, vector = found
        # `>=`, not `>`, so a TTL of zero means "do not cache" rather than "cache
        # anything read in the same instant" — which is what a test setting it to zero
        # is asking for, and what an operator setting it to zero would expect.
        if time.monotonic() - stored_at >= self.ttl_seconds:
            del self._entries[key]
            return None
        return vector

    def put(self, model: str, text: str, vector: Sequence[float]) -> None:
        if len(self._entries) >= self.max_entries:
            # `next(iter(...))` is the oldest key: dicts preserve insertion order.
            del self._entries[next(iter(self._entries))]
        self._entries[(model, text)] = (time.monotonic(), list(vector))


# ---------------------------------------------------------------------------
# the retriever
# ---------------------------------------------------------------------------


class Retriever:
    """Query text in, chunks out, inside a deadline."""

    def __init__(
        self,
        embedder: Embedder,
        vectors: VectorStore,
        *,
        cache: QueryCache | None = None,
        metrics: RetrievalMetrics | None = None,
    ) -> None:
        self._embedder = embedder
        self._vectors = vectors
        self._cache = cache if cache is not None else QueryCache()
        self._metrics = metrics

    async def documents(
        self,
        *,
        organization_id: uuid.UUID,
        config: MemoryConfig,
        messages: Sequence[ChatMessage],
    ) -> Retrieval:
        """Document memory for one request. Never raises; see the module docstring."""
        if not config.connector_ids:
            # SPEC §6.3: empty means no document memory. Skipped before the query is even
            # built, so an unconfigured gateway pays nothing at all for the feature.
            return self._done(NOTHING)

        query = build_query(messages, config)
        if not query:
            # A conversation with no user turn — a bare system message, or an assistant
            # prefill. There is nothing to be similar to.
            return self._done(Retrieval(outcome=SKIPPED))

        started = time.perf_counter()
        try:
            async with asyncio.timeout(config.retrieval_timeout_ms / 1000):
                matches = await self._search(organization_id, config, query)
        except TimeoutError:
            logger.warning(
                "retrieval timed out",
                extra={
                    "organization_id": str(organization_id),
                    "timeout_ms": config.retrieval_timeout_ms,
                },
            )
            return self._done(
                Retrieval(
                    outcome=TIMEOUT,
                    latency_ms=_ms(started),
                    error=(
                        f"The knowledge base did not answer within "
                        f"{config.retrieval_timeout_ms} ms."
                    ),
                    query=query,
                )
            )
        except _DimensionMismatch as exc:
            # Loud on purpose. Under `fail_open` this is otherwise invisible: every
            # request serves fine and simply has no documents in it.
            logger.error(
                "the vector index was built by a different embedding model; retrieval is off",
                extra={
                    "organization_id": str(organization_id),
                    "collection_dimension": exc.found,
                    "embedder_dimension": exc.expected,
                },
            )
            return self._done(
                Retrieval(
                    outcome=ERROR,
                    latency_ms=_ms(started),
                    error=(
                        f"The document index was built with {exc.found}-dimensional vectors "
                        f"and the configured embedding model produces {exc.expected}. "
                        f"Re-index this organization's connectors before retrieval can work."
                    ),
                    query=query,
                )
            )
        except Exception as exc:
            # One handler for both halves on purpose. An embedding provider's 500 and a
            # Qdrant that is restarting are the same event as far as this request is
            # concerned: memory is unavailable, and `on_retrieval_error` decides what that
            # means. The distinction that *does* matter — which one it was — goes to the
            # log rather than to the caller, whose next step is identical either way.
            logger.warning(
                "retrieval failed",
                extra={"organization_id": str(organization_id), "error": type(exc).__name__},
                exc_info=True,
            )
            return self._done(
                Retrieval(
                    outcome=ERROR,
                    latency_ms=_ms(started),
                    error="The knowledge base could not be reached for this request.",
                    query=query,
                )
            )

        chunks = tuple(Chunk.of(match) for match in matches)
        return self._done(
            Retrieval(
                chunks=chunks,
                outcome=HIT if chunks else EMPTY,
                latency_ms=_ms(started),
                query=query,
            )
        )

    async def _search(
        self, organization_id: uuid.UUID, config: MemoryConfig, query: str
    ) -> list[Match]:
        expected = self._embedder.dimension
        found = await self._vectors.dimension(organization_id)
        if found is None:
            # No collection: nothing in this organization has ever been indexed. Not an
            # error, and not worth an embedding call.
            return []
        if found != expected:
            raise _DimensionMismatch(expected=expected, found=found)

        vector = await self._embed(query)
        matches = await self._vectors.search(
            organization_id,
            vector,
            # SPEC §5.3, defence in depth. The collection is already per organization, so
            # this filter cannot be the thing that keeps tenants apart — but it is what
            # keeps *gateways* apart, and a gateway reading a connector nobody attached to
            # it is the same disclosure bug one tenancy layer down.
            connector_ids=list(config.connector_ids),
            limit=config.doc_top_k,
            # Pushed into the store so `doc_top_k` means "this many usable chunks" rather
            # than "this many candidates, some of which will be discarded".
            min_score=config.doc_min_score,
        )
        return _dedupe(matches)

    async def _embed(self, query: str) -> list[float]:
        model = self._embedder.model
        if (cached := self._cache.get(model, query)) is not None:
            return cached
        vector = (await self._embedder.embed([query]))[0]
        self._cache.put(model, query, vector)
        return list(vector)

    def _done(self, retrieval: Retrieval) -> Retrieval:
        if self._metrics is not None:
            self._metrics.attempts.labels(outcome=retrieval.outcome).inc()
            if retrieval.attempted:
                self._metrics.duration.observe(retrieval.latency_ms / 1000)
        return retrieval


class _DimensionMismatch(Exception):
    def __init__(self, *, expected: int, found: int) -> None:
        super().__init__(f"collection is {found}-dimensional, embedder is {expected}")
        self.expected = expected
        self.found = found


def _dedupe(matches: Sequence[Match]) -> list[Match]:
    """Drop a chunk that neighbours one already kept from the same document.

    Chunks overlap by design — 150 tokens of 1000, by default — so consecutive chunks of
    the same file share about a sixth of their text and almost always score alike. Two of
    them in a prompt spends the token budget twice on one passage and pushes a genuinely
    different document out of the block. Keeping the higher-scoring one of a pair loses
    little: they are near-identical, and the sentence at the boundary is in both.

    Matches arrive sorted by score, so the first of a neighbouring pair seen here is the
    better one.
    """
    kept: list[Match] = []
    for match in matches:
        document = match.payload.get("document_id")
        index = int(match.payload.get("chunk_index", 0) or 0)
        if any(
            existing.payload.get("document_id") == document
            and abs(int(existing.payload.get("chunk_index", 0) or 0) - index) <= 1
            for existing in kept
        ):
            continue
        kept.append(match)
    return kept


def _as_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


# ---------------------------------------------------------------------------
# both halves of memory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Recall:
    """Everything the memory subsystem found for one request.

    Two fields, one of which is always empty in this build. ``facts`` is SPEC §6.3's
    conversation memory, which task 12 fills; it is here now so that the assembler, the
    request log and the response headers already have somewhere to read it from, and so
    the layer-4 hole in :mod:`app.services.prompt` is filled by passing a value rather
    than by changing a signature.
    """

    documents: Retrieval = NOTHING
    facts: tuple[str, ...] = ()

    @property
    def latency_ms(self) -> int:
        """Wall clock for the whole recall, not the sum of its branches.

        They run concurrently, so adding them would report a number nobody waited.
        """
        return self.documents.latency_ms


class MemoryService:
    """The concurrent fan-out SPEC §6.3 asks for: documents and facts, at the same time.

    Today one branch is real and the other returns immediately. That is the point of
    writing it as a :func:`asyncio.gather` now rather than later: task 12's conversation
    memory is a second independent network call under its own timeout, and the shape that
    absorbs it without restructuring the request path is this one. The cost of the empty
    branch is a coroutine that returns a constant — several hundred nanoseconds, against a
    call that is allowed 800 milliseconds.
    """

    def __init__(self, retriever: Retriever, *, metrics: RetrievalMetrics | None = None) -> None:
        self._retriever = retriever
        self._metrics = metrics

    async def recall(
        self,
        *,
        organization_id: uuid.UUID,
        config: MemoryConfig,
        messages: Sequence[ChatMessage],
    ) -> Recall:
        documents, facts = await asyncio.gather(
            self._retriever.documents(
                organization_id=organization_id, config=config, messages=messages
            ),
            self._facts(config),
        )
        return Recall(documents=documents, facts=facts)

    async def _facts(self, config: MemoryConfig) -> tuple[str, ...]:
        """Layer 4. Task 12 replaces the body; the branch already exists."""
        return ()

    def injected(self, tokens: int) -> None:
        """What memory actually cost this request, in tokens.

        Recorded here rather than inside :class:`Retriever` because retrieval does not
        know the answer: the budget is applied by the assembler, against the context
        window of whichever target answered. And recorded once per *request* rather than
        once per routing attempt — a failover assembles the same chunks twice, and
        counting both would report a token bill nobody was sent.
        """
        if self._metrics is not None:
            self._metrics.injected_tokens.observe(tokens)


__all__ = [
    "EMPTY",
    "ERROR",
    "HIT",
    "MAX_QUERY_CHARS",
    "NOTHING",
    "SKIPPED",
    "TIMEOUT",
    "Chunk",
    "MemoryService",
    "QueryCache",
    "Recall",
    "Retrieval",
    "RetrievalUnavailable",
    "Retriever",
    "build_query",
]
