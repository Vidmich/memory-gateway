"""The memory browser's rules (SPEC §6.5, §13.1).

What an operator may see and do about the people on the far side of their gateways: who
has been asking, what the assistant believes about them, and how to correct or erase it.

Four decisions run through this module.

**Manual entry is the whole point of shipping recall before distillation.** Task 13 writes
facts automatically from transcripts; until it exists, :meth:`EndUserService.create_fact`
is what makes the injection path demonstrable, and it stays afterwards because "the model
keeps forgetting that we are in the EU" needs an answer that does not involve waiting for
a distillation pass. A hand-written fact gets ``confidence = 1.0``, which is not
flattery — a person typed it, and nothing the model infers should outrank that.

**Two systems change together, and the order is chosen for the failure.** A fact is a row
in PostgreSQL and a point in Qdrant. Writes go row-then-vector, so a crash between them
leaves a fact that is visible, editable and reachable through the always-include set but
not through similarity — degraded, and obviously so. Deletes go vector-then-row, so a
crash leaves a row with no vector rather than a point with no row: the first is a fact the
operator can see and delete again, the second is an orphan nothing in the UI can reach.
Erasure is a promise, and the half that must not be left behind is the half that is not on
screen.

**Purge does not delete the end user.** SPEC §6.5's erasure is about *memory* — facts,
their vectors, and optionally the transcripts they were distilled from. The ``end_users``
row is attribution: it is what makes yesterday's request log say who a request belonged
to, and removing it would rewrite a record of things that happened rather than forget what
was learned from them. The confirmation copy says exactly that, so nobody presses it
expecting the other thing.

**Search here is the same search recall does.** The memory browser's box embeds with the
same embedder and filters on the same ``end_user_id``; it is the fastest way to see why a
fact that obviously answers the question is not being recalled, and a second, similar-
looking implementation would be a screen that agrees with itself and disagrees with the
request path.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import Conflict, NotFound, Validation
from app.core.tenancy import Actor
from app.db.models import EndUser, MemoryFact
from app.db.models.end_user import FACT_KINDS, MAX_FACT_LENGTH
from app.schemas.distillation import organization_distillation
from app.services.embeddings import Embedder
from app.services.end_user_store import EndUserStore, EndUserTransaction, FactDraft, FactPatch
from app.services.fact_vectors import FactPoint, FactVectorStore, fact_payload
from app.services.metrics_store import MetricsRepository
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of

logger = logging.getLogger(__name__)

#: Same answer for "no such end user" and "belongs to another organization" — see
#: ``tests/test_cross_tenant.py``.
NO_SUCH_END_USER = "No such end user."
NO_SUCH_FACT = "No such fact."

#: Longest search query accepted by the memory browser's box.
MAX_SEARCH_QUERY = 1000

#: How many candidates a browser search asks the index for, at most.
MAX_SEARCH_RESULTS = 50


@dataclass(frozen=True, slots=True)
class EndUserView:
    """A row plus the number nobody wants to page through the facts to find."""

    end_user: EndUser
    fact_count: int


@dataclass(frozen=True, slots=True)
class FactHit:
    """A fact and how well it matched. The browser's search result."""

    fact: MemoryFact
    score: float


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What an erasure actually removed, so the confirmation can say it."""

    facts: int
    transcripts: int


class EndUserService:
    def __init__(
        self,
        store: EndUserStore,
        *,
        vectors: FactVectorStore,
        embedder: Embedder,
        logs: MetricsRepository | None = None,
    ) -> None:
        self._store = store
        self._vectors = vectors
        self._embedder = embedder
        self._logs = logs

    @property
    def embedding_model(self) -> str:
        return self._embedder.model

    # -- reads -----------------------------------------------------------

    async def list_end_users(
        self,
        actor: Actor,
        *,
        search: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[EndUserView]:
        size = clamp_limit(limit)
        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.end_users(
                after=decode_cursor(cursor), limit=size, search=_clean_search(search)
            )
            page = page_of(list(rows), limit=size, cursor_of=lambda row: row.id)
            counts = await transaction.fact_counts([row.id for row in page.items])
        return Page(
            items=tuple(
                EndUserView(end_user=row, fact_count=counts.get(row.id, 0)) for row in page.items
            ),
            next_cursor=page.next_cursor,
        )

    async def get_end_user(self, actor: Actor, end_user_id: uuid.UUID) -> EndUserView:
        async with self._store.begin(actor.scope) as transaction:
            row = await self._require(transaction, end_user_id)
            return EndUserView(end_user=row, fact_count=await transaction.count_facts(end_user_id))

    async def list_facts(
        self,
        actor: Actor,
        end_user_id: uuid.UUID,
        *,
        live_only: bool = False,
        kind: str | None = None,
        min_confidence: float | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[MemoryFact]:
        """One end user's facts, newest first.

        Superseded and expired rows are included by default. They are not noise: "why did
        it say that last month" is answered by the fact that has since been replaced, and
        a browser that showed only live memory could not answer it at all.

        ``kind`` and ``min_confidence`` narrow it. An unknown kind is a 422 rather than an
        empty page: "constraints" instead of "constraint" would otherwise read as "this
        person has no constraints", which is the wrong answer to a question about what the
        assistant must respect.
        """
        if kind is not None:
            _check_kind(kind)
        if min_confidence is not None:
            _check_confidence(min_confidence)
        size = clamp_limit(limit)
        async with self._store.begin(actor.scope) as transaction:
            await self._require(transaction, end_user_id)
            rows = await transaction.facts(
                end_user_id,
                after=decode_cursor(cursor),
                limit=size,
                live_only=live_only,
                kind=kind,
                min_confidence=min_confidence,
            )
        return page_of(list(rows), limit=size, cursor_of=lambda row: row.id)

    async def search_facts(
        self, actor: Actor, end_user_id: uuid.UUID, query: str, *, limit: int = 10
    ) -> list[FactHit]:
        """Semantic search over one end user's memory — the same search recall runs."""
        text = _clean_query(query)
        async with self._store.begin(actor.scope) as transaction:
            row = await self._require(transaction, end_user_id)
            organization_id = row.organization_id

        if await self._vectors.dimension(organization_id) is None:
            # Nothing has ever been remembered here, so there is no collection to search.
            return []

        vector = (await self._embedder.embed([text]))[0]
        matches = await self._vectors.search(
            organization_id,
            list(vector),
            end_user_id=end_user_id,
            limit=max(1, min(limit, MAX_SEARCH_RESULTS)),
        )
        if not matches:
            return []

        scores = {match.id: match.score for match in matches}
        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.live_facts(
                end_user_id,
                [parsed for raw in scores if (parsed := _as_uuid(raw)) is not None],
                now=datetime.now(UTC),
            )
        hits = [FactHit(fact=fact, score=scores.get(str(fact.id), 0.0)) for fact in rows]
        hits.sort(key=lambda hit: (-hit.score, str(hit.fact.id)))
        return hits

    # -- writes ----------------------------------------------------------

    async def create_fact(
        self,
        actor: Actor,
        end_user_id: uuid.UUID,
        *,
        text: str,
        kind: str = "fact",
        confidence: float = 1.0,
        expires_at: datetime | None = None,
    ) -> MemoryFact:
        cleaned = _clean_fact(text)
        _check_kind(kind)
        _check_confidence(confidence)
        if expires_at is not None:
            _check_expiry(expires_at)

        async with self._store.begin(actor.scope) as transaction:
            end_user = await self._require(transaction, end_user_id)
            organization_id = end_user.organization_id
            # The bound is the organization's, not this service's and not a gateway's: an
            # end user reaches an organization through however many endpoints it has, and
            # a per-endpoint cap on how much may be known about one person is not a cap.
            bound = organization_distillation(
                await transaction.organization_settings(organization_id)
            ).max_facts_per_user
            if await transaction.count_facts(end_user_id) >= bound:
                raise Conflict(
                    f"This user already has the maximum of {bound} facts. "
                    f"Delete or supersede one before adding another."
                )
            fact = await transaction.add_fact(
                end_user,
                FactDraft(text=cleaned, kind=kind, confidence=confidence, expires_at=expires_at),
            )
            await transaction.commit()

        try:
            await self._index(organization_id, fact)
        except Exception:
            # Roll the row back by hand: there is no transaction spanning both systems,
            # and a fact that exists but can never be found by similarity is worse than
            # one the operator is told did not save.
            logger.warning(
                "could not index a new fact; removing the row",
                extra={"organization_id": str(organization_id), "fact_id": str(fact.id)},
                exc_info=True,
            )
            async with self._store.begin(actor.scope) as transaction:
                stored = await transaction.fact(fact.id)
                if stored is not None:
                    await transaction.delete_fact(stored)
                await transaction.commit()
            raise
        return fact

    async def update_fact(self, actor: Actor, fact_id: uuid.UUID, patch: FactPatch) -> MemoryFact:
        if patch.kind is not None:
            _check_kind(patch.kind)
        if patch.confidence is not None:
            _check_confidence(patch.confidence)
        if patch.expires_at is not None and not patch.clear_expiry:
            _check_expiry(patch.expires_at)

        async with self._store.begin(actor.scope) as transaction:
            fact = await transaction.fact(fact_id)
            if fact is None:
                raise NotFound(NO_SUCH_FACT)
            organization_id = fact.organization_id

            if patch.text is not None:
                fact.text = _clean_fact(patch.text)
            if patch.kind is not None:
                fact.kind = patch.kind
            if patch.confidence is not None:
                fact.confidence = patch.confidence
            if patch.clear_expiry:
                fact.expires_at = None
            elif patch.expires_at is not None:
                fact.expires_at = patch.expires_at
            if patch.superseded is not None:
                fact.superseded_at = datetime.now(UTC) if patch.superseded else None
            if patch.text is not None or patch.confidence is not None:
                # An edit is an observation: somebody looked at this fact today and said
                # it is still what they mean. Without this the recency decay would go on
                # ageing a sentence that was just confirmed.
                fact.last_seen_at = datetime.now(UTC)
            await transaction.commit()
            superseded = fact.superseded_at is not None

        if superseded:
            # A superseded fact keeps its row and loses its vector, so recall cannot reach
            # it even if a liveness filter is later written wrongly. Two independent
            # mechanisms for the one rule this feature cannot get wrong.
            await self._vectors.delete(organization_id, [fact_id])
        else:
            # Re-indexed unconditionally rather than only when the text changed. The extra
            # embedding is one call on an action a person performs by hand, and the branch
            # it removes — "did this edit affect the vector or only the payload" — is the
            # kind that is right until somebody adds a field.
            await self._index(organization_id, fact)
        return fact

    async def delete_fact(self, actor: Actor, fact_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            fact = await transaction.fact(fact_id)
            if fact is None:
                raise NotFound(NO_SUCH_FACT)
            organization_id = fact.organization_id

        # Vector first: see the module docstring on ordering.
        await self._vectors.delete(organization_id, [fact_id])
        async with self._store.begin(actor.scope) as transaction:
            stored = await transaction.fact(fact_id)
            if stored is not None:
                await transaction.delete_fact(stored)
            await transaction.commit()

    async def purge(
        self, actor: Actor, end_user_id: uuid.UUID, *, include_transcripts: bool = False
    ) -> PurgeResult:
        """SPEC §6.5. Removes facts and their vectors; optionally the transcripts too."""
        async with self._store.begin(actor.scope) as transaction:
            end_user = await self._require(transaction, end_user_id)
            organization_id = end_user.organization_id

        # By filter, not by the ids we think exist: a purge that missed a point nobody
        # remembered would be a right-to-erasure failure, and the filter cannot miss one.
        await self._vectors.delete_end_user(organization_id, end_user_id)

        async with self._store.begin(actor.scope) as transaction:
            removed = await transaction.delete_facts_of(end_user_id)
            await transaction.commit()

        transcripts = 0
        if include_transcripts and self._logs is not None:
            async with self._logs.begin(actor.scope) as reader:
                transcripts = await reader.erase_transcripts(end_user_id)

        logger.info(
            "purged conversation memory",
            extra={
                "organization_id": str(organization_id),
                "end_user_id": str(end_user_id),
                "facts": removed,
                "transcripts": transcripts,
                "audit_action": "end_user.memory.purge",
            },
        )
        return PurgeResult(facts=removed, transcripts=transcripts)

    # -- internals -------------------------------------------------------

    async def _index(self, organization_id: uuid.UUID, fact: MemoryFact) -> None:
        vector = (await self._embedder.embed([fact.text]))[0]
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

    @staticmethod
    async def _require(transaction: EndUserTransaction, end_user_id: uuid.UUID) -> EndUser:
        found = await transaction.end_user(end_user_id)
        if found is None:
            raise NotFound(NO_SUCH_END_USER)
        return found


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _clean_fact(text: str) -> str:
    """A fact is one sentence, and this is where that is enforced.

    Newlines are collapsed rather than rejected. A fact pasted out of a document arrives
    with them, and the block that renders it is a bulleted list where a newline would
    close the list visually — so flattening here means the assembler never has to be the
    only thing standing between a paste and a prompt that reads as two sections.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        raise Validation("A fact needs some text.", param="text")
    if len(cleaned) > MAX_FACT_LENGTH:
        raise Validation(
            f"A fact is at most {MAX_FACT_LENGTH} characters. Anything longer is a summary "
            f"of a conversation rather than something durable about this person.",
            param="text",
        )
    return cleaned


def _check_kind(kind: str) -> None:
    if kind not in FACT_KINDS:
        raise Validation(f"Kind must be one of: {', '.join(FACT_KINDS)}.", param="kind")


def _check_confidence(confidence: float) -> None:
    if not 0.0 <= confidence <= 1.0:
        raise Validation("Confidence is a fraction between 0 and 1.", param="confidence")


def _check_expiry(expires_at: datetime) -> None:
    if _aware(expires_at) <= datetime.now(UTC):
        raise Validation(
            "An expiry in the past would hide the fact the moment it is saved.",
            param="expires_at",
        )


def _clean_search(value: str | None) -> str | None:
    text = (value or "").strip()
    return text[:MAX_SEARCH_QUERY] or None


def _clean_query(value: str) -> str:
    text = value.strip()
    if not text:
        raise Validation("Type something to search for.", param="query")
    return text[:MAX_SEARCH_QUERY]


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _epoch(value: datetime) -> float:
    return _aware(value).timestamp()


__all__ = [
    "MAX_SEARCH_QUERY",
    "MAX_SEARCH_RESULTS",
    "NO_SUCH_END_USER",
    "NO_SUCH_FACT",
    "EndUserService",
    "EndUserView",
    "FactHit",
    "PurgeResult",
]
