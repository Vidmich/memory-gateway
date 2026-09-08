"""Conversation memory, on the way in (SPEC §6.3, layer 4).

The half of memory that is about a *person* rather than about a corpus. Documents are
shared by everyone who calls a gateway; facts are private to one
``(organization, end_user)`` pair, and every decision here follows from that.

**Similarity alone is the wrong retrieval.** A dense search for "how should I store
customer emails?" will find "prefers Python" long before it finds "works in the EU and
needs GDPR-compliant answers", because the second shares almost no vocabulary with the
question and the first shares a topic word. But the second is the one that changes the
answer. So recall is two reads: a similarity search, and an **always-include** set — the
most recently seen high-confidence facts, whatever they are about. "Uses metric units" and
"is a minor" have to reach every turn, and no query will ever be similar to them.

**Ranking is ``similarity * confidence * recency``**, and the third factor is the one worth
arguing about. A fact decays on ``last_seen_at`` rather than ``created_at``: a preference
stated two years ago and restated last week is current, and reading the creation date would
bury it under something newer and less true. The half-life is
:data:`RECENCY_HALF_LIFE_DAYS` — ninety days, tuned once and stated here rather than made
configurable, because it is a property of how fast people change rather than of any one
customer's deployment, and a per-gateway knob would be a number nobody has the data to set.

**The vector store is an index; PostgreSQL is the record.** The search returns ids, and the
rows come from ``memory_facts`` with the liveness predicate applied in SQL. That is what
makes "a superseded fact is never injected" a property of one ``WHERE`` clause rather than
of a payload staying in step with a row it cannot see. See
:mod:`app.services.fact_vectors` for the other half of that argument.

**Nothing recalled here is trusted.** It originated in an end user's own conversation, so
it is rendered inside a delimited block as data — :mod:`app.services.prompt` puts it under
its own heading, after the document block and before the client's own system message, and
never in a position where a sentence beginning "ignore your instructions" would read as
one. That matters more once task 13 writes these facts automatically from whatever
somebody typed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.api.proxy.errors import GatewayUnavailable
from app.core.metrics import RetrievalMetrics
from app.core.tenancy import TenantScope
from app.db.models import MemoryFact
from app.schemas.gateway_config import MemoryConfig
from app.services.embeddings import Embedder
from app.services.end_user_store import EndUserStore
from app.services.fact_vectors import FactVectorStore

logger = logging.getLogger(__name__)

#: Outcomes, matching :mod:`app.services.retrieval` so one dashboard can put the two
#: memories side by side. ``skipped`` is the interesting one here and it has four causes,
#: which is why :attr:`FactRecall.reason` exists next to it.
SKIPPED = "skipped"
HIT = "hit"
EMPTY = "empty"
TIMEOUT = "timeout"
ERROR = "error"

FAILED_OUTCOMES = frozenset({TIMEOUT, ERROR})

#: Why nothing was recalled, when nothing was. These are the four questions a customer
#: asks as "why does it not remember me", and they have four different answers: the
#: gateway has memory switched off, the caller sent no identity, the caller is anonymous
#: and this gateway does not keep anonymous memory, or there is simply nothing stored yet.
DISABLED = "memory_disabled"
NO_IDENTITY = "no_identity"
ANONYMOUS_NOT_ALLOWED = "anonymous_not_allowed"
NOTHING_STORED = "nothing_stored"

#: How long a fact keeps half its weight. Ninety days is a statement about people, not
#: about a deployment: long enough that a preference expressed once and never contradicted
#: still counts a quarter later, short enough that a year-old goal stops out-ranking this
#: month's. It multiplies the score rather than filtering, so an old fact that is the only
#: match is still recalled.
RECENCY_HALF_LIFE_DAYS = 90.0

#: How many high-confidence facts are included whatever the question. Three, because the
#: block has a token budget and these spend it before anything similarity found: they are
#: the standing constraints — a language, a unit system, a legal jurisdiction — and one or
#: two is the usual number a person actually has.
ALWAYS_INCLUDE_COUNT = 3

#: The confidence a fact needs to be included regardless of similarity. High on purpose: a
#: guess the distillation model was unsure about should have to earn its place by matching
#: the question, and 0.8 is roughly "the model stated this rather than inferred it".
ALWAYS_INCLUDE_MIN_CONFIDENCE = 0.8

#: How many candidates the vector search asks for, as a multiple of ``memory_top_k``.
#: Superseded facts lose their vector at the moment they are superseded, so the only rows
#: the SQL filter drops afterwards are *expired* ones — rare enough that two is generous
#: and cheap enough that the alternative, a second round trip when the first came back
#: short, is not worth having.
CANDIDATE_MULTIPLIER = 2

#: Why a fact was recalled but not injected. Recorded per fact on the request log, so
#: "it knows I am in the EU and answered as if I were not" has an answer.
DROPPED_BUDGET = "memory_max_tokens"


@dataclass(frozen=True, slots=True)
class Fact:
    """One recalled fact, flattened out of a row and its score.

    A dataclass rather than the ORM row for the same reason :class:`
    ~app.services.retrieval.Chunk` is one: the assembler, the request log and the editor's
    preview all read it, and none of them should be holding a live session's object.
    """

    id: str
    text: str
    kind: str
    confidence: float
    #: Cosine similarity to the query, or ``0.0`` for a fact that arrived through the
    #: always-include set and was never scored against anything.
    similarity: float
    #: ``similarity * confidence * recency``. What the ordering is on.
    score: float
    #: True when this fact is here because it is recent and confident rather than because
    #: it matched. Shown in the drawer, because "why is this in my prompt" has two answers.
    always: bool
    last_seen_at: datetime

    @classmethod
    def of(cls, row: MemoryFact, *, similarity: float, always: bool, now: datetime) -> Fact:
        return cls(
            id=str(row.id),
            text=row.text,
            kind=row.kind,
            confidence=float(row.confidence),
            similarity=similarity,
            score=rank(
                similarity=similarity,
                confidence=float(row.confidence),
                last_seen_at=row.last_seen_at,
                now=now,
            ),
            always=always,
            last_seen_at=row.last_seen_at,
        )

    def as_log_entry(self, *, injected: bool, dropped_reason: str | None = None) -> dict[str, Any]:
        """The row that goes into ``request_logs.retrieved_fact_ids``.

        The *text* is stored, not only the id, and for the same reason the chunk log
        stores its source name: a fact edited or deleted next week must not turn last
        week's explanation of an answer into a dangling id.
        """
        entry: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "score": round(self.score, 6),
            "similarity": round(self.similarity, 6),
            "confidence": round(self.confidence, 4),
            "always": self.always,
            "injected": injected,
        }
        if dropped_reason is not None:
            entry["dropped"] = dropped_reason
        return entry


def recency(last_seen_at: datetime, now: datetime) -> float:
    """Exponential decay on age, halving every :data:`RECENCY_HALF_LIFE_DAYS`.

    Clamped at 1.0 for anything not in the past, because clock skew between the writer
    and the reader should not be able to make a fact score *above* a fresh one.
    """
    age_days = max(0.0, (now - _aware(last_seen_at)).total_seconds() / 86_400.0)
    return math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)


def rank(*, similarity: float, confidence: float, last_seen_at: datetime, now: datetime) -> float:
    """SPEC §6.3's ``similarity * confidence * recency_decay``, in one place.

    A product rather than a weighted sum: each factor is a fraction of "how much should
    this count", and a fact that fails any one of them badly should not be rescued by the
    other two. A sum with weights would let a very recent, very confident fact about
    something else outrank the passage that actually answers the question.
    """
    return max(0.0, similarity) * max(0.0, confidence) * recency(last_seen_at, now)


@dataclass(frozen=True, slots=True)
class FactRecall:
    """What conversation memory found for one request, including the nothing.

    Mirrors :class:`~app.services.retrieval.Retrieval` field for field where it can, so
    the request path, the log and the editor handle both memories the same way.
    """

    facts: tuple[Fact, ...] = ()
    outcome: str = SKIPPED
    latency_ms: int = 0
    error: str | None = None
    #: Which end user this was, when there was one. Copied onto the request log.
    end_user_id: uuid.UUID | None = None
    #: One of the four constants above when ``outcome`` is ``skipped``. ``None`` otherwise.
    reason: str | None = None

    @property
    def failed(self) -> bool:
        return self.outcome in FAILED_OUTCOMES

    @property
    def attempted(self) -> bool:
        return self.outcome != SKIPPED

    def enforce(self, policy: str) -> None:
        """Apply ``on_retrieval_error``, exactly as document retrieval does.

        SPEC §6.3 gives the two memories independent timeouts and one policy, so a
        gateway configured to refuse ungrounded answers refuses them when it is the
        *user's* memory that is unreachable too. The sentence says which half failed,
        because the operator's next step is different.
        """
        if self.failed and policy == "fail_closed":
            verb = "timed out" if self.outcome == TIMEOUT else "failed"
            raise MemoryUnavailable(
                f"This gateway is configured to refuse requests it cannot ground, and "
                f"conversation-memory recall {verb}. {self.error or ''}".strip()
            )


class MemoryUnavailable(GatewayUnavailable):
    """``on_retrieval_error = fail_closed`` and conversation memory did not answer."""


NO_FACTS = FactRecall()


class FactRecaller:
    """Query text plus an end user in, ranked facts out, inside a deadline.

    Never raises for a recall failure — the outcome is on the result and the *caller*
    applies the gateway's policy, which is what lets the editor render the same failure as
    a diagnostic instead of a 503.
    """

    def __init__(
        self,
        embedder: Embedder,
        vectors: FactVectorStore,
        store: EndUserStore,
        *,
        metrics: RetrievalMetrics | None = None,
    ) -> None:
        self._embedder = embedder
        self._vectors = vectors
        self._store = store
        self._metrics = metrics

    async def recall(
        self,
        *,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID | None,
        config: MemoryConfig,
        query: str,
        embed: Callable[[], Awaitable[Sequence[float]]] | None = None,
        identity_reason: str = NO_IDENTITY,
    ) -> FactRecall:
        """Facts for one request. ``embed`` produces the query vector when one is needed.

        A callable rather than a vector, so the two halves of memory embed the same
        question **once** — :class:`~app.services.retrieval.QueryCache` hands the second
        caller the first caller's in-flight task — and so a recall that turns out not to
        need a vector at all never starts one. Omitted, this embeds for itself, which is
        what a test and the editor's preview want.
        """
        if not config.memory_enabled:
            return self._done(FactRecall(outcome=SKIPPED, reason=DISABLED))
        if end_user_id is None:
            # Either nobody sent an identity or the gateway declines to invent one for an
            # anonymous caller. Only the route can tell those apart — it is the thing
            # holding the headers and the gateway's settings — so it says which, and the
            # difference matters: one is fixed by the customer's integration and the other
            # by a checkbox on this gateway.
            return self._done(FactRecall(outcome=SKIPPED, reason=identity_reason))
        if not query:
            # No user turn to be similar to. The always-include set could still be read,
            # but a request with no question is a prefill or a bare system message, and
            # spending a round trip on it buys nothing.
            return self._done(FactRecall(outcome=SKIPPED, end_user_id=end_user_id))

        started = time.perf_counter()
        try:
            async with asyncio.timeout(config.retrieval_timeout_ms / 1000):
                facts = await self._gather(organization_id, end_user_id, config, query, embed)
        except TimeoutError:
            logger.warning(
                "conversation-memory recall timed out",
                extra={
                    "organization_id": str(organization_id),
                    "timeout_ms": config.retrieval_timeout_ms,
                },
            )
            return self._done(
                FactRecall(
                    outcome=TIMEOUT,
                    latency_ms=_ms(started),
                    end_user_id=end_user_id,
                    error=(
                        f"Conversation memory did not answer within "
                        f"{config.retrieval_timeout_ms} ms."
                    ),
                )
            )
        except Exception as exc:
            logger.warning(
                "conversation-memory recall failed",
                extra={"organization_id": str(organization_id), "error": type(exc).__name__},
                exc_info=True,
            )
            return self._done(
                FactRecall(
                    outcome=ERROR,
                    latency_ms=_ms(started),
                    end_user_id=end_user_id,
                    error="This user's memory could not be read for this request.",
                )
            )

        return self._done(
            FactRecall(
                facts=facts,
                outcome=HIT if facts else EMPTY,
                latency_ms=_ms(started),
                end_user_id=end_user_id,
                reason=None if facts else NOTHING_STORED,
            )
        )

    # -- internals --------------------------------------------------------

    async def _gather(
        self,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        config: MemoryConfig,
        query: str,
        embed: Callable[[], Awaitable[Sequence[float]]] | None,
    ) -> tuple[Fact, ...]:
        now = datetime.now(UTC)
        similar, always = await asyncio.gather(
            self._similar(organization_id, end_user_id, config, query, embed),
            self._always(organization_id, end_user_id, now),
        )
        return _merge(similar, always, now=now, limit=config.memory_top_k)

    async def _similar(
        self,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        config: MemoryConfig,
        query: str,
        embed: Callable[[], Awaitable[Sequence[float]]] | None,
    ) -> list[tuple[MemoryFact, float]]:
        found = await self._vectors.dimension(organization_id)
        if found is None:
            # Nothing has ever been remembered in this organization. Not an error, and
            # not worth an embedding call.
            return []
        if found != self._embedder.dimension:
            # The same failure the document index has, and the same reasoning for making
            # it loud: under `fail_open` it is otherwise invisible.
            logger.error(
                "the memory index was built by a different embedding model; recall is off",
                extra={
                    "organization_id": str(organization_id),
                    "collection_dimension": found,
                    "embedder_dimension": self._embedder.dimension,
                },
            )
            return []

        vector = await embed() if embed is not None else (await self._embedder.embed([query]))[0]
        matches = await self._vectors.search(
            organization_id,
            list(vector),
            end_user_id=end_user_id,
            limit=config.memory_top_k * CANDIDATE_MULTIPLIER,
            min_score=config.memory_min_score,
        )
        if not matches:
            return []

        scores = {match.id: match.score for match in matches}
        rows = await self._rows(organization_id, end_user_id, list(scores))
        return [(row, scores.get(str(row.id), 0.0)) for row in rows]

    async def _always(
        self, organization_id: uuid.UUID, end_user_id: uuid.UUID, now: datetime
    ) -> list[MemoryFact]:
        async with self._store.begin(TenantScope.of_organization(organization_id)) as tx:
            return list(
                await tx.recent_facts(
                    end_user_id,
                    limit=ALWAYS_INCLUDE_COUNT,
                    min_confidence=ALWAYS_INCLUDE_MIN_CONFIDENCE,
                    now=now,
                )
            )

    async def _rows(
        self, organization_id: uuid.UUID, end_user_id: uuid.UUID, ids: Sequence[str]
    ) -> list[MemoryFact]:
        parsed = [value for value in (_as_uuid(raw) for raw in ids) if value is not None]
        async with self._store.begin(TenantScope.of_organization(organization_id)) as tx:
            return list(await tx.live_facts(end_user_id, parsed, now=datetime.now(UTC)))

    def _done(self, recall: FactRecall) -> FactRecall:
        if self._metrics is not None:
            self._metrics.recalls.labels(outcome=recall.outcome).inc()
            if recall.attempted:
                self._metrics.recall_duration.observe(recall.latency_ms / 1000)
        return recall


def _merge(
    similar: Sequence[tuple[MemoryFact, float]],
    always: Sequence[MemoryFact],
    *,
    now: datetime,
    limit: int,
) -> tuple[Fact, ...]:
    """Always-include first, then the rest by score, deduplicated by id.

    The order is the acceptance criterion, not a preference. Truncation — by
    ``memory_top_k`` here and by ``memory_max_tokens`` in the assembler — always takes
    from the tail, so putting the always-include set at the head is what makes "these
    apply to every turn" true rather than merely likely. A fact that is both recent and
    similar keeps its similarity score and its place at the front.
    """
    scored = {str(row.id): value for row, value in similar}
    seen: set[str] = set()
    facts: list[Fact] = []

    for row in always:
        key = str(row.id)
        seen.add(key)
        facts.append(Fact.of(row, similarity=scored.get(key, 0.0), always=True, now=now))

    rest = [
        Fact.of(row, similarity=value, always=False, now=now)
        for row, value in similar
        if str(row.id) not in seen
    ]
    rest.sort(key=lambda fact: (-fact.score, fact.id))
    facts.extend(rest)
    return tuple(facts[:limit])


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        # A point id that is not a fact id means the collection holds something this
        # build did not write. Skipping it is right; failing the request is not.
        return None


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    The in-memory store stamps aware datetimes and PostgreSQL returns them, so this only
    ever fires for a row somebody built by hand in a test — where raising would be a
    confusing way to say "you forgot a timezone".
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


__all__ = [
    "ALWAYS_INCLUDE_COUNT",
    "ALWAYS_INCLUDE_MIN_CONFIDENCE",
    "ANONYMOUS_NOT_ALLOWED",
    "CANDIDATE_MULTIPLIER",
    "DISABLED",
    "DROPPED_BUDGET",
    "EMPTY",
    "ERROR",
    "HIT",
    "NOTHING_STORED",
    "NO_FACTS",
    "NO_IDENTITY",
    "RECENCY_HALF_LIFE_DAYS",
    "SKIPPED",
    "TIMEOUT",
    "Fact",
    "FactRecall",
    "FactRecaller",
    "MemoryUnavailable",
    "rank",
    "recency",
]
