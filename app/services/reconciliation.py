"""Merging what a model proposed into what is already believed (SPEC §6.4, steps 3-5).

Extraction is the part that looks hard and reconciliation is the part that is. A model
handed the same conversation twice returns roughly the same sentences, phrased differently
both times; a person who changes their mind states the new position without mentioning the
old one; and a memory that only ever grows ends as a prompt holding two contradictory
sentences with no way to tell which is current. Every rule here exists because of one of
those three.

**Similarity decides sameness; the model decides contradiction.** Two sentences above
``dedupe_threshold`` are the same fact said again — reinforced, not duplicated. A
contradiction is not similar in the same way ("prefers Rust" and "prefers Go" share a
structure, not a meaning), so it cannot be detected by distance, and the extractor names it
explicitly in ``supersedes``. Trying to infer contradiction from similarity is how "works in
Berlin" silently reinforces "works in Munich".

**Deduplication happens against this pass as well as against the index.** A pass can
propose two phrasings of one fact, and the second one's search would only find the first if
Qdrant had already indexed it — which is a promise no vector store makes within
milliseconds. So each newly written vector is also kept in hand and compared directly, and
the index is consulted for everything older. Without that, a busy conversation writes the
same sentence twice on the same pass, reliably, and only sometimes.

**The whole pass for one person runs under a lock.** Two jobs for the same end user —a
retry overlapping its original, "distil now" pressed while a debounced pass is in flight —
would otherwise both search, both find nothing, and both insert. The lock is a convenience
and not the correctness mechanism: it can expire under a stalled holder, which is why every
write below is also safe to repeat. What repeating costs is one duplicate fact, which the
next pass then deduplicates.

**Eviction takes the dead first, and there are two budgets.** ``max_facts_per_user``
bounds what is *live* — what recall can actually reach, and the number the memory browser
refuses a hand-written fact against. Superseded rows are not subject to it, because a
retraction must never make room by pushing out a live fact; they are the answer to "why did
it say that last month", and that is worth keeping. But they also accumulate forever, so
they get a budget of their own at :data:`HISTORY_MULTIPLE` times the bound. Eviction walks
:func:`worst_first` — dead rows before live ones, then SPEC §6.4's
``confidence * recency_decay``, the same decay recall ranks with — and stops as soon as both
budgets are met. So a memory full of history spends its eviction on the history, and a live
fact is only ever forgotten when there are too many live facts.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.core.tenancy import TenantScope
from app.db.models import MemoryFact
from app.services.distillation import Candidate
from app.services.embeddings import Embedder
from app.services.end_user_store import EndUserStore, EndUserTransaction, FactDraft, is_live
from app.services.fact_vectors import FactPoint, FactVectorStore, fact_payload
from app.services.facts import rank
from app.services.locks import Lock
from app.services.vector_store import cosine

logger = logging.getLogger(__name__)

#: How much history one person may keep, as a multiple of the live bound. Superseded facts
#: are what the browser answers "why did it say that last month" with, so they are worth
#: keeping — but not forever, and not without a number. Two means a person at their bound
#: also carries a bound's worth of what they used to believe.
HISTORY_MULTIPLE = 2

#: How long the per-user lock is held at most. A pass is a handful of small queries plus
#: some embeddings; a minute is far more than it needs and short enough that a worker
#: killed mid-pass does not block the next one for long.
LOCK_TTL_SECONDS = 60


def lock_key(end_user_id: uuid.UUID) -> str:
    """One pass at a time per *person*, not per session. Two threads of the same person
    write into one memory, and the fact they collide on is the one they have in common."""
    return f"distil:{end_user_id}"


@dataclass(frozen=True, slots=True)
class Reconciled:
    """What a pass did to somebody's memory."""

    inserted: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    #: Ids of the facts written, so the caller can log what a conversation produced.
    written: tuple[uuid.UUID, ...] = ()
    #: True when another pass held the lock and this one did nothing.
    contended: bool = False


class Reconciler:
    def __init__(
        self,
        store: EndUserStore,
        *,
        vectors: FactVectorStore,
        embedder: Embedder,
        lock: Lock,
    ) -> None:
        self._store = store
        self._vectors = vectors
        self._embedder = embedder
        self._lock = lock

    async def apply(
        self,
        *,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        candidates: Sequence[Candidate],
        dedupe_threshold: float,
        max_facts_per_user: int,
        source_log_id: uuid.UUID | None = None,
    ) -> Reconciled:
        if not candidates:
            # Still worth entering: nothing to write does not mean nothing to evict, and a
            # bound lowered on the Settings screen has to take effect on the next pass
            # rather than only on the next pass that happens to learn something.
            async with self._lock.hold(lock_key(end_user_id), ttl_seconds=LOCK_TTL_SECONDS) as held:
                if not held:
                    return Reconciled(contended=True)
                return Reconciled(
                    evicted=await self._evict(organization_id, end_user_id, max_facts_per_user)
                )

        vectors = await self._embedder.embed([candidate.text for candidate in candidates])

        async with self._lock.hold(lock_key(end_user_id), ttl_seconds=LOCK_TTL_SECONDS) as held:
            if not held:
                logger.info(
                    "another distillation pass holds this user; leaving it to that one",
                    extra={"end_user_id": str(end_user_id)},
                )
                return Reconciled(contended=True)

            result = Reconciled()
            #: Vectors written during *this* pass, compared directly — see the module
            #: docstring on why the index alone is not enough.
            fresh: list[tuple[uuid.UUID, list[float]]] = []
            for candidate, vector in zip(candidates, vectors, strict=True):
                result = await self._one(
                    result,
                    organization_id=organization_id,
                    end_user_id=end_user_id,
                    candidate=candidate,
                    vector=list(vector),
                    fresh=fresh,
                    dedupe_threshold=dedupe_threshold,
                    source_log_id=source_log_id,
                )

            evicted = await self._evict(organization_id, end_user_id, max_facts_per_user)
            return replace(result, evicted=evicted)

    # -- one candidate ----------------------------------------------------

    async def _one(
        self,
        result: Reconciled,
        *,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        candidate: Candidate,
        vector: list[float],
        fresh: list[tuple[uuid.UUID, list[float]]],
        dedupe_threshold: float,
        source_log_id: uuid.UUID | None,
    ) -> Reconciled:
        scope = TenantScope.of_organization(organization_id)
        existing = await self._same_fact(
            organization_id, end_user_id, vector, fresh, dedupe_threshold
        )

        if existing is not None:
            async with self._store.begin(scope) as transaction:
                row = await transaction.fact(existing)
                if row is not None and is_live(row, datetime.now(UTC)):
                    await transaction.observe(
                        row, confidence=candidate.confidence, seen_at=datetime.now(UTC)
                    )
                    await transaction.commit()
                    # The vector is unchanged — same text, same embedding — but the payload
                    # carries the confidence a filter could one day read, so it is rewritten
                    # rather than left describing the old number.
                    await self._index(organization_id, row, vector)
                    return replace(result, deduped=result.deduped + 1)

        async with self._store.begin(scope) as transaction:
            end_user = await transaction.end_user(end_user_id)
            if end_user is None:
                # Purged between the extraction and the write. Nothing to attach a fact to,
                # and inventing the row would undo an erasure somebody performed.
                logger.info(
                    "end user disappeared mid-pass; dropping a candidate",
                    extra={"end_user_id": str(end_user_id)},
                )
                return result
            written = await transaction.add_fact(
                end_user,
                FactDraft(
                    text=candidate.text,
                    kind=candidate.kind,
                    confidence=candidate.confidence,
                    expires_at=_expiry(candidate.ttl_days),
                    source_log_id=source_log_id,
                ),
            )
            retired = await self._retire(
                transaction, end_user_id, candidate.supersedes, replacement_id=written.id
            )
            await transaction.commit()

        await self._index(organization_id, written, vector)
        fresh.append((written.id, vector))
        if retired:
            # A superseded fact keeps its row and loses its vector, so recall cannot reach
            # it even if a liveness filter is one day written wrongly.
            await self._vectors.delete(organization_id, retired)
        return replace(
            result,
            inserted=result.inserted + 1,
            superseded=result.superseded + len(retired),
            written=(*result.written, written.id),
        )

    @staticmethod
    async def _retire(
        transaction: EndUserTransaction,
        end_user_id: uuid.UUID,
        supersedes: Sequence[uuid.UUID],
        *,
        replacement_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """Mark what this fact replaces, and say which rows actually changed.

        The ids are re-read through the store rather than trusted from the extraction:
        :func:`app.services.distillation.parse` has already dropped anything outside this
        end user's facts, and this is the second, independent check of the same rule — the
        one that runs against the database rather than against a set built in memory.
        """
        if not supersedes:
            return []
        rows = await transaction.facts_by_id(end_user_id, list(supersedes))
        now = datetime.now(UTC)
        retired: list[uuid.UUID] = []
        for row in rows:
            if row.superseded_at is not None:
                continue
            await transaction.supersede(row, replacement_id=replacement_id, at=now)
            retired.append(row.id)
        return retired

    async def _same_fact(
        self,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        vector: list[float],
        fresh: Sequence[tuple[uuid.UUID, list[float]]],
        threshold: float,
    ) -> uuid.UUID | None:
        for fact_id, written in fresh:
            if cosine(vector, written) >= threshold:
                return fact_id
        if await self._vectors.dimension(organization_id) is None:
            return None
        matches = await self._vectors.search(
            organization_id, vector, end_user_id=end_user_id, limit=1, min_score=threshold
        )
        if not matches:
            return None
        try:
            return uuid.UUID(matches[0].id)
        except (ValueError, AttributeError, TypeError):
            return None

    # -- bounding ---------------------------------------------------------

    async def _evict(self, organization_id: uuid.UUID, end_user_id: uuid.UUID, bound: int) -> int:
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            rows = list(await transaction.all_facts(end_user_id))

        ids = [fact.id for fact in over_budget(rows, bound=bound)]
        if not ids:
            return 0
        # Vector first, as everywhere else here: a crash leaves a row with no vector, which
        # an operator can see and delete, rather than a point with no row, which nothing in
        # the UI can reach.
        await self._vectors.delete(organization_id, ids)
        async with self._store.begin(scope) as transaction:
            for fact_id in ids:
                stored = await transaction.fact(fact_id)
                if stored is not None:
                    await transaction.delete_fact(stored)
            await transaction.commit()

        logger.info(
            "evicted facts to stay within the per-user bound",
            extra={
                "organization_id": str(organization_id),
                "end_user_id": str(end_user_id),
                "evicted": len(ids),
                "bound": bound,
            },
        )
        return len(ids)

    async def _index(
        self, organization_id: uuid.UUID, fact: MemoryFact, vector: Sequence[float]
    ) -> None:
        await self._vectors.ensure_collection(organization_id, dimension=self._embedder.dimension)
        await self._vectors.upsert(
            organization_id,
            [
                FactPoint(
                    id=str(fact.id),
                    vector=list(vector),
                    payload=fact_payload(
                        organization_id=organization_id,
                        end_user_id=fact.end_user_id,
                        kind=fact.kind,
                        confidence=float(fact.confidence),
                        created_at=_epoch(fact.created_at),
                    ),
                )
            ],
        )


@dataclass(frozen=True, slots=True)
class Ranked:
    """A fact and what it is worth keeping, for the eviction order."""

    fact: MemoryFact
    score: float
    live: bool


def worst_first(rows: Sequence[MemoryFact], *, now: datetime | None = None) -> list[Ranked]:
    """Everything about one person, in the order it would be forgotten.

    Dead rows first whatever their score — a bound reached by a memory full of history
    should spend its eviction on the history — then live ones by ``confidence * recency``.
    The similarity factor of :func:`app.services.facts.rank` is fixed at 1.0 here because
    there is no query: eviction asks what a fact is worth in general, not what it is worth
    to one question.
    """
    moment = now or datetime.now(UTC)
    ranked = [
        Ranked(
            fact=row,
            score=rank(
                similarity=1.0,
                confidence=float(row.confidence),
                last_seen_at=row.last_seen_at,
                now=moment,
            ),
            live=is_live(row, moment),
        )
        for row in rows
    ]
    ranked.sort(key=lambda item: (item.live, item.score, str(item.fact.id)))
    return ranked


def over_budget(
    rows: Sequence[MemoryFact], *, bound: int, now: datetime | None = None
) -> list[MemoryFact]:
    """Which facts have to go, in the order they go.

    Two budgets, checked together: at most ``bound`` live facts, and at most
    ``bound * HISTORY_MULTIPLE`` rows in total. Walking :func:`worst_first` means dead rows
    are considered before live ones, so a memory over its total budget loses history before
    it loses anything the assistant is still using — and a live fact is skipped entirely
    while the live count is already within its bound, so trimming history can never cost
    somebody a preference they still hold.
    """
    ranked = worst_first(rows, now=now)
    remaining_total = len(ranked)
    remaining_live = sum(1 for entry in ranked if entry.live)
    history_budget = bound * HISTORY_MULTIPLE

    doomed: list[MemoryFact] = []
    for entry in ranked:
        if remaining_live <= bound and remaining_total <= history_budget:
            break
        if entry.live and remaining_live <= bound:
            continue
        doomed.append(entry.fact)
        remaining_total -= 1
        if entry.live:
            remaining_live -= 1
    return doomed


def _expiry(ttl_days: int | None) -> datetime | None:
    return None if ttl_days is None else datetime.now(UTC) + timedelta(days=ttl_days)


def _epoch(value: datetime) -> float:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).timestamp()


__all__ = [
    "HISTORY_MULTIPLE",
    "LOCK_TTL_SECONDS",
    "Ranked",
    "Reconciled",
    "Reconciler",
    "lock_key",
    "over_budget",
    "worst_first",
]
