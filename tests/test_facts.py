"""Conversation-memory recall: ranking, isolation, and the two ways it can find nothing.

Everything here runs against the real :class:`~app.services.facts.FactRecaller` over the
in-memory store and the in-memory fact index — the second implementation of each port,
not a mock. That matters for the isolation assertions above all: "alice's facts never
reach bob" is a claim about a filter chain, and a double that returns a canned list would
prove nothing about it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import CollectorRegistry

from app.core.metrics import build_retrieval_metrics
from app.schemas.gateway_config import MemoryConfig
from app.services.end_user_store import FactPatch
from app.services.facts import (
    ALWAYS_INCLUDE_COUNT,
    ANONYMOUS_NOT_ALLOWED,
    DISABLED,
    EMPTY,
    ERROR,
    HIT,
    NO_IDENTITY,
    NOTHING_STORED,
    RECENCY_HALF_LIFE_DAYS,
    SKIPPED,
    TIMEOUT,
    FactRecall,
    FactRecaller,
    MemoryUnavailable,
    rank,
    recency,
)
from tests.auth_support import make_organization
from tests.end_user_support import EndUserFixture, build_end_users


@pytest.fixture
def memory() -> EndUserFixture:
    return build_end_users(make_organization())


def config(**overrides: object) -> MemoryConfig:
    return MemoryConfig.load({"memory_min_score": 0.0, **overrides})


async def recall(
    fixture: EndUserFixture, end_user_id: uuid.UUID | None, query: str, **overrides: object
) -> FactRecall:
    return await fixture.recaller.recall(
        organization_id=fixture.organization_id,
        end_user_id=end_user_id,
        config=config(**overrides),
        query=query,
    )


def texts(result: FactRecall) -> list[str]:
    return [fact.text for fact in result.facts]


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def test_recency_halves_every_half_life() -> None:
    now = datetime.now(UTC)

    assert recency(now, now) == pytest.approx(1.0)
    assert recency(now - timedelta(days=RECENCY_HALF_LIFE_DAYS), now) == pytest.approx(0.5)
    assert recency(now - timedelta(days=2 * RECENCY_HALF_LIFE_DAYS), now) == pytest.approx(0.25)


def test_a_clock_that_ran_ahead_cannot_score_above_a_fresh_fact() -> None:
    """Skew between the writer and the reader must not make a fact worth more than 1."""
    now = datetime.now(UTC)

    assert recency(now + timedelta(days=30), now) == pytest.approx(1.0)


def test_the_rank_is_the_product_of_all_three() -> None:
    now = datetime.now(UTC)
    stale = now - timedelta(days=RECENCY_HALF_LIFE_DAYS)

    assert rank(similarity=0.8, confidence=0.5, last_seen_at=now, now=now) == pytest.approx(0.4)
    assert rank(similarity=0.8, confidence=1.0, last_seen_at=stale, now=now) == pytest.approx(0.4)


def test_a_confident_recent_fact_about_something_else_does_not_outrank_the_answer() -> None:
    """A product, not a weighted sum: failing one factor badly is not rescued by the
    other two."""
    now = datetime.now(UTC)
    off_topic = rank(similarity=0.05, confidence=1.0, last_seen_at=now, now=now)
    on_topic = rank(similarity=0.7, confidence=0.6, last_seen_at=now - timedelta(days=60), now=now)

    assert on_topic > off_topic


async def test_facts_come_back_ordered_by_score(memory: EndUserFixture) -> None:
    """Below the always-include confidence, so ordering is purely what similarity said."""
    alice = await memory.end_user("alice")
    await memory.remember(
        alice, "Refunds are handled by the billing team.", "Likes cats.", confidence=0.5
    )

    result = await recall(memory, alice.id, "refunds billing")

    assert result.outcome == HIT
    assert not any(fact.always for fact in result.facts)
    assert result.facts[0].text == "Refunds are handled by the billing team."
    assert result.facts[0].score >= result.facts[-1].score


async def test_an_older_fact_is_still_recalled_when_it_is_the_only_match(
    memory: EndUserFixture,
) -> None:
    """Recency multiplies the score; it does not filter. A fact from two years ago that
    is the only thing answering the question still arrives."""
    alice = await memory.end_user("alice")
    (fact,) = await memory.remember(alice, "Uses the Zynthorp QX-4471.")
    async with memory.store.begin(memory.actor.scope) as transaction:
        row = await transaction.fact(fact.id)
        assert row is not None
        row.last_seen_at = datetime.now(UTC) - timedelta(days=700)
        await transaction.commit()

    result = await recall(memory, alice.id, "Zynthorp QX-4471")

    assert texts(result) == ["Uses the Zynthorp QX-4471."]


# ---------------------------------------------------------------------------
# always-include
# ---------------------------------------------------------------------------


async def test_a_standing_constraint_survives_a_query_with_no_similarity_to_it(
    memory: EndUserFixture,
) -> None:
    """The acceptance criterion, and the reason recall is two reads rather than one.

    "Works in the EU and needs GDPR-compliant answers" shares no vocabulary with "how do
    I store customer emails", and it is the fact that changes the answer.
    """
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Works in the EU and needs GDPR-compliant answers.")

    result = await recall(memory, alice.id, "how should I store customer emails?")

    assert texts(result) == ["Works in the EU and needs GDPR-compliant answers."]
    assert result.facts[0].always


async def test_always_included_facts_come_first(memory: EndUserFixture) -> None:
    """Truncation takes from the tail — by ``memory_top_k`` here and by the token budget
    in the assembler — so the order is what makes "applies to every turn" hold."""
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Prefers examples in Python.", confidence=0.5)
    await memory.remember(alice, "Uses metric units.", confidence=1.0)

    result = await recall(memory, alice.id, "examples in Python please")

    assert result.facts[0].text == "Uses metric units."
    assert result.facts[0].always


async def test_an_unconfident_fact_is_not_always_included(memory: EndUserFixture) -> None:
    """A guess the distiller was unsure about has to earn its place by matching the
    question, rather than riding along on every turn the way a stated fact does."""
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Might live in Berlin.", confidence=0.4)

    result = await recall(memory, alice.id, "Berlin")

    assert texts(result) == ["Might live in Berlin."]
    assert not result.facts[0].always


async def test_the_score_floor_keeps_a_weak_match_out(memory: EndUserFixture) -> None:
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Might live in Berlin.", confidence=0.4)

    result = await recall(memory, alice.id, "Berlin", memory_min_score=0.99)

    assert texts(result) == []


async def test_at_most_a_handful_of_facts_are_always_included(
    memory: EndUserFixture,
) -> None:
    alice = await memory.end_user("alice")
    await memory.remember(alice, *[f"Standing constraint {index}." for index in range(6)])

    result = await recall(memory, alice.id, "something else entirely")

    assert sum(1 for fact in result.facts if fact.always) == ALWAYS_INCLUDE_COUNT


async def test_top_k_bounds_the_whole_result(memory: EndUserFixture) -> None:
    alice = await memory.end_user("alice")
    await memory.remember(alice, *[f"Fact about refunds number {index}." for index in range(8)])

    result = await recall(memory, alice.id, "refunds", memory_top_k=2)

    assert len(result.facts) == 2


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


async def test_one_persons_facts_are_never_recalled_for_another(
    memory: EndUserFixture,
) -> None:
    """The acceptance criterion, end to end through the real filter chain."""
    alice = await memory.end_user("alice")
    bob = await memory.end_user("bob")
    await memory.remember(alice, "Works in the EU and needs GDPR-compliant answers.")
    await memory.remember(bob, "Works in the United States.")

    hers = await recall(memory, alice.id, "how should I store customer emails?")
    his = await recall(memory, bob.id, "how should I store customer emails?")

    assert texts(hers) == ["Works in the EU and needs GDPR-compliant answers."]
    assert texts(his) == ["Works in the United States."]


async def test_memory_does_not_cross_organizations(memory: EndUserFixture) -> None:
    """Two organizations, one shared index object, both end users called ``alice``."""
    other = build_end_users(
        make_organization(name="Globex", slug="globex"),
        database=memory.database,
        vectors=memory.vectors,
        embedder=memory.embedder,
    )
    mine = await memory.end_user("alice")
    theirs = await other.end_user("alice")
    await memory.remember(mine, "Acme's customer likes concise answers.")
    await other.remember(theirs, "Globex's customer likes long answers.")

    result = await recall(memory, mine.id, "answers")

    assert texts(result) == ["Acme's customer likes concise answers."]


async def test_a_superseded_fact_is_never_injected(memory: EndUserFixture) -> None:
    alice = await memory.end_user("alice")
    (fact,) = await memory.remember(alice, "Lives in Munich.")
    await memory.service.update_fact(memory.actor, fact.id, FactPatch(superseded=True))

    result = await recall(memory, alice.id, "Munich")

    assert texts(result) == []


async def test_an_expired_fact_is_never_injected(memory: EndUserFixture) -> None:
    alice = await memory.end_user("alice")
    (fact,) = await memory.remember(alice, "Is travelling until Tuesday.")
    async with memory.store.begin(memory.actor.scope) as transaction:
        row = await transaction.fact(fact.id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await transaction.commit()

    result = await recall(memory, alice.id, "travelling Tuesday")

    assert texts(result) == []


# ---------------------------------------------------------------------------
# the four ways to find nothing
# ---------------------------------------------------------------------------


async def test_memory_switched_off_says_so(memory: EndUserFixture) -> None:
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Prefers Python.")

    result = await recall(memory, alice.id, "python", memory_enabled=False)

    assert (result.outcome, result.reason) == (SKIPPED, DISABLED)


async def test_no_identity_says_so(memory: EndUserFixture) -> None:
    result = await recall(memory, None, "anything")

    assert (result.outcome, result.reason) == (SKIPPED, NO_IDENTITY)


async def test_an_anonymous_caller_on_a_gateway_that_refuses_them_says_which(
    memory: EndUserFixture,
) -> None:
    """Two causes of "it does not remember me", and they need different fixes: the
    customer's integration, or a checkbox on this gateway."""
    result = await memory.recaller.recall(
        organization_id=memory.organization_id,
        end_user_id=None,
        config=config(),
        query="anything",
        identity_reason=ANONYMOUS_NOT_ALLOWED,
    )

    assert (result.outcome, result.reason) == (SKIPPED, ANONYMOUS_NOT_ALLOWED)


async def test_a_known_person_with_nothing_stored_is_empty_rather_than_skipped(
    memory: EndUserFixture,
) -> None:
    alice = await memory.end_user("alice")

    result = await recall(memory, alice.id, "anything")

    assert (result.outcome, result.reason) == (EMPTY, NOTHING_STORED)
    assert result.end_user_id == alice.id


async def test_a_request_with_no_question_is_skipped(memory: EndUserFixture) -> None:
    alice = await memory.end_user("alice")
    await memory.remember(alice, "Prefers Python.")

    result = await recall(memory, alice.id, "")

    assert result.outcome == SKIPPED


# ---------------------------------------------------------------------------
# failure
# ---------------------------------------------------------------------------


class SlowVectors:
    """A fact index that never answers in time."""

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay

    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        return 64

    async def search(self, *args: object, **kwargs: object) -> list[object]:
        await asyncio.sleep(self.delay)
        return []


class BrokenVectors:
    async def dimension(self, organization_id: uuid.UUID) -> int | None:
        return 64

    async def search(self, *args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("the index is on fire")


async def test_a_slow_index_becomes_a_timeout_not_an_exception(
    memory: EndUserFixture,
) -> None:
    alice = await memory.end_user("alice")
    recaller = FactRecaller(memory.embedder, SlowVectors(), memory.store)  # type: ignore[arg-type]

    result = await recaller.recall(
        organization_id=memory.organization_id,
        end_user_id=alice.id,
        config=config(retrieval_timeout_ms=50),
        query="anything",
    )

    assert result.outcome == TIMEOUT
    assert result.error is not None
    assert "50 ms" in result.error


async def test_a_broken_index_becomes_an_error_not_an_exception(
    memory: EndUserFixture,
) -> None:
    alice = await memory.end_user("alice")
    recaller = FactRecaller(memory.embedder, BrokenVectors(), memory.store)  # type: ignore[arg-type]

    result = await recaller.recall(
        organization_id=memory.organization_id,
        end_user_id=alice.id,
        config=config(),
        query="anything",
    )

    assert result.outcome == ERROR
    # The provider's own words never reach the caller; they go to the structured log.
    assert result.error == "This user's memory could not be read for this request."


def test_fail_open_lets_a_failed_recall_through() -> None:
    FactRecall(outcome=TIMEOUT).enforce("fail_open")


def test_fail_closed_refuses_and_names_the_half_that_failed() -> None:
    with pytest.raises(MemoryUnavailable) as caught:
        FactRecall(outcome=TIMEOUT, error="…").enforce("fail_closed")

    assert "conversation-memory recall timed out" in str(caught.value)


def test_a_successful_recall_is_never_refused() -> None:
    FactRecall(outcome=EMPTY).enforce("fail_closed")


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


async def test_every_outcome_is_counted(memory: EndUserFixture) -> None:
    registry = CollectorRegistry()
    metrics = build_retrieval_metrics(registry)
    recaller = FactRecaller(memory.embedder, memory.vectors, memory.store, metrics=metrics)
    alice = await memory.end_user("alice")

    await recaller.recall(
        organization_id=memory.organization_id,
        end_user_id=alice.id,
        config=config(),
        query="anything",
    )

    assert registry.get_sample_value("memory_recalls_total", {"outcome": EMPTY}) == 1.0
