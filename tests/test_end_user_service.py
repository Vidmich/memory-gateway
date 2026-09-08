"""The memory browser's rules: manual entry, correction, and erasure.

Every check here drives the real :class:`~app.services.end_users.EndUserService` over the
in-memory store and the in-memory fact index, so an assertion about a purge is an
assertion about *both* systems rather than about one of them.

The file's centre of gravity is erasure. SPEC §6.5 is a promise to somebody's customer,
and the way it goes wrong in practice is not that the endpoint fails — it is that one of
the two stores is left holding something, and nothing on any screen would say so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models.end_user import MAX_FACT_LENGTH
from app.services.end_user_store import FactPatch
from tests.auth_support import make_organization
from tests.end_user_support import EndUserFixture, build_end_users
from tests.monitoring_support import make_log_row


@pytest.fixture
def memory() -> EndUserFixture:
    return build_end_users(make_organization())


async def alice(fixture: EndUserFixture) -> uuid.UUID:
    row = await fixture.end_user("alice")
    return row.id


# ---------------------------------------------------------------------------
# manual entry
# ---------------------------------------------------------------------------


async def test_a_hand_written_fact_is_stored_and_indexed(memory: EndUserFixture) -> None:
    """Both halves. A row nothing can find by similarity is a fact the assistant will
    only recall by accident."""
    who = await alice(memory)

    fact = await memory.service.create_fact(
        memory.actor, who, text="Works in the EU and needs GDPR-compliant answers."
    )

    assert fact.confidence == 1.0
    assert await memory.vectors.count(memory.organization_id, end_user_id=who) == 1


async def test_a_fact_is_flattened_to_one_line(memory: EndUserFixture) -> None:
    """A paste out of a document arrives with newlines, and the block that renders it is
    a bulleted list where one would close the list visually."""
    who = await alice(memory)

    fact = await memory.service.create_fact(
        memory.actor, who, text="Works in\n\nthe EU.   Needs GDPR answers."
    )

    assert fact.text == "Works in the EU. Needs GDPR answers."


async def test_an_empty_fact_is_refused(memory: EndUserFixture) -> None:
    who = await alice(memory)

    with pytest.raises(Validation):
        await memory.service.create_fact(memory.actor, who, text="   \n  ")


async def test_a_fact_longer_than_a_sentence_is_refused(memory: EndUserFixture) -> None:
    who = await alice(memory)

    with pytest.raises(Validation):
        await memory.service.create_fact(memory.actor, who, text="x" * (MAX_FACT_LENGTH + 1))


async def test_an_unknown_kind_is_refused(memory: EndUserFixture) -> None:
    who = await alice(memory)

    with pytest.raises(Validation):
        await memory.service.create_fact(memory.actor, who, text="Something.", kind="rumour")


async def test_an_expiry_in_the_past_is_refused(memory: EndUserFixture) -> None:
    """It would hide the fact the moment it is saved, which reads as the save failing."""
    who = await alice(memory)

    with pytest.raises(Validation):
        await memory.service.create_fact(
            memory.actor,
            who,
            text="Is travelling.",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )


async def test_a_fact_for_another_organization_is_not_found(memory: EndUserFixture) -> None:
    other = build_end_users(
        make_organization(name="Globex", slug="globex"), database=memory.database
    )
    theirs = await other.end_user("alice")

    with pytest.raises(NotFound):
        await memory.service.create_fact(memory.actor, theirs.id, text="Owned.")


async def test_the_per_user_cap_is_enforced(memory: EndUserFixture) -> None:
    """A person's durable memory that has grown past the cap has stopped being memory."""
    small = build_end_users(make_organization(name="Small", slug="small"), max_facts_per_user=2)
    who = (await small.end_user("alice")).id
    await small.service.create_fact(small.actor, who, text="One.")
    await small.service.create_fact(small.actor, who, text="Two.")

    with pytest.raises(Conflict):
        await small.service.create_fact(small.actor, who, text="Three.")


async def test_a_retracted_fact_does_not_count_against_the_cap(
    memory: EndUserFixture,
) -> None:
    small = build_end_users(make_organization(name="Small", slug="small"), max_facts_per_user=1)
    who = (await small.end_user("alice")).id
    first = await small.service.create_fact(small.actor, who, text="One.")
    await small.service.update_fact(small.actor, first.id, FactPatch(superseded=True))

    await small.service.create_fact(small.actor, who, text="Two.")


# ---------------------------------------------------------------------------
# correction
# ---------------------------------------------------------------------------


async def test_editing_the_text_reindexes_it(memory: EndUserFixture) -> None:
    who = await alice(memory)
    fact = await memory.service.create_fact(memory.actor, who, text="Lives in Munich.")

    await memory.service.update_fact(memory.actor, fact.id, FactPatch(text="Lives in Berlin."))
    hits = await memory.service.search_facts(memory.actor, who, "Berlin")

    assert [hit.fact.text for hit in hits] == ["Lives in Berlin."]


async def test_retracting_a_fact_keeps_the_row_and_removes_the_vector(
    memory: EndUserFixture,
) -> None:
    """Two independent mechanisms for the one rule this feature cannot get wrong: recall
    filters superseded rows in SQL, *and* there is no vector left to reach them by."""
    who = await alice(memory)
    fact = await memory.service.create_fact(memory.actor, who, text="Lives in Munich.")

    updated = await memory.service.update_fact(memory.actor, fact.id, FactPatch(superseded=True))

    assert updated.superseded_at is not None
    assert await memory.vectors.count(memory.organization_id, end_user_id=who) == 0
    page = await memory.service.list_facts(memory.actor, who)
    assert [row.id for row in page.items] == [fact.id]


async def test_bringing_a_retracted_fact_back_reindexes_it(memory: EndUserFixture) -> None:
    who = await alice(memory)
    fact = await memory.service.create_fact(memory.actor, who, text="Lives in Munich.")
    await memory.service.update_fact(memory.actor, fact.id, FactPatch(superseded=True))

    await memory.service.update_fact(memory.actor, fact.id, FactPatch(superseded=False))

    assert await memory.vectors.count(memory.organization_id, end_user_id=who) == 1


async def test_editing_a_fact_counts_as_seeing_it_again(memory: EndUserFixture) -> None:
    """Without this the recency decay would go on ageing a sentence somebody just
    confirmed."""
    who = await alice(memory)
    fact = await memory.service.create_fact(memory.actor, who, text="Lives in Munich.")
    async with memory.store.begin(memory.actor.scope) as transaction:
        row = await transaction.fact(fact.id)
        assert row is not None
        row.last_seen_at = datetime.now(UTC) - timedelta(days=365)
        await transaction.commit()

    updated = await memory.service.update_fact(
        memory.actor, fact.id, FactPatch(text="Lives in Berlin.")
    )

    assert updated.last_seen_at > datetime.now(UTC) - timedelta(minutes=1)


async def test_clearing_an_expiry_is_distinct_from_not_mentioning_it(
    memory: EndUserFixture,
) -> None:
    who = await alice(memory)
    fact = await memory.service.create_fact(
        memory.actor,
        who,
        text="Is travelling.",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )

    untouched = await memory.service.update_fact(
        memory.actor, fact.id, FactPatch(text="Is still travelling.")
    )
    assert untouched.expires_at is not None

    cleared = await memory.service.update_fact(memory.actor, fact.id, FactPatch(clear_expiry=True))
    assert cleared.expires_at is None


async def test_editing_a_fact_in_another_organization_is_not_found(
    memory: EndUserFixture,
) -> None:
    other = build_end_users(
        make_organization(name="Globex", slug="globex"), database=memory.database
    )
    theirs = await other.end_user("alice")
    fact = await other.service.create_fact(other.actor, theirs.id, text="Globex only.")

    with pytest.raises(NotFound):
        await memory.service.update_fact(memory.actor, fact.id, FactPatch(text="Owned."))


async def test_deleting_a_fact_removes_the_row_and_the_vector(
    memory: EndUserFixture,
) -> None:
    who = await alice(memory)
    fact = await memory.service.create_fact(memory.actor, who, text="Lives in Munich.")

    await memory.service.delete_fact(memory.actor, fact.id)

    assert await memory.vectors.count(memory.organization_id, end_user_id=who) == 0
    page = await memory.service.list_facts(memory.actor, who)
    assert page.items == ()


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


async def test_the_list_carries_a_live_fact_count(memory: EndUserFixture) -> None:
    who = await alice(memory)
    await memory.service.create_fact(memory.actor, who, text="One.")
    retracted = await memory.service.create_fact(memory.actor, who, text="Two.")
    await memory.service.update_fact(memory.actor, retracted.id, FactPatch(superseded=True))

    page = await memory.service.list_end_users(memory.actor)

    assert [(view.end_user.external_id, view.fact_count) for view in page.items] == [("alice", 1)]


async def test_the_list_can_be_searched(memory: EndUserFixture) -> None:
    await memory.end_user("alice@example.com")
    await memory.end_user("bob@example.com")

    page = await memory.service.list_end_users(memory.actor, search="alice")

    assert [view.end_user.external_id for view in page.items] == ["alice@example.com"]


async def test_a_list_never_includes_another_organization(memory: EndUserFixture) -> None:
    other = build_end_users(
        make_organization(name="Globex", slug="globex"), database=memory.database
    )
    await other.end_user("theirs")
    await memory.end_user("mine")

    page = await memory.service.list_end_users(memory.actor)

    assert [view.end_user.external_id for view in page.items] == ["mine"]


async def test_searching_a_persons_memory_is_the_same_search_recall_runs(
    memory: EndUserFixture,
) -> None:
    who = await alice(memory)
    await memory.service.create_fact(memory.actor, who, text="Refunds go to the billing team.")
    await memory.service.create_fact(memory.actor, who, text="Likes cats.")

    hits = await memory.service.search_facts(memory.actor, who, "refunds billing")

    assert hits[0].fact.text == "Refunds go to the billing team."
    assert hits[0].score > 0


async def test_searching_a_person_with_no_memory_is_not_an_error(
    memory: EndUserFixture,
) -> None:
    who = await alice(memory)

    assert await memory.service.search_facts(memory.actor, who, "anything") == []


async def test_search_never_returns_a_retracted_fact(memory: EndUserFixture) -> None:
    who = await alice(memory)
    fact = await memory.service.create_fact(memory.actor, who, text="Lives in Munich.")
    await memory.service.update_fact(memory.actor, fact.id, FactPatch(superseded=True))

    assert await memory.service.search_facts(memory.actor, who, "Munich") == []


async def test_an_empty_search_is_refused(memory: EndUserFixture) -> None:
    who = await alice(memory)

    with pytest.raises(Validation):
        await memory.service.search_facts(memory.actor, who, "   ")


# ---------------------------------------------------------------------------
# erasure
# ---------------------------------------------------------------------------


async def test_a_purge_empties_both_stores(memory: EndUserFixture) -> None:
    """The acceptance criterion. Zero rows *and* zero points, and a recall afterwards
    that finds nothing."""
    who = await alice(memory)
    await memory.service.create_fact(memory.actor, who, text="Works in the EU.")
    await memory.service.create_fact(memory.actor, who, text="Prefers Python.")

    result = await memory.service.purge(memory.actor, who)

    assert result.facts == 2
    assert await memory.vectors.count(memory.organization_id, end_user_id=who) == 0
    page = await memory.service.list_facts(memory.actor, who)
    assert page.items == ()

    from app.schemas.gateway_config import MemoryConfig

    recall = await memory.recaller.recall(
        organization_id=memory.organization_id,
        end_user_id=who,
        config=MemoryConfig.load({"memory_min_score": 0.0}),
        query="the EU",
    )
    assert recall.facts == ()


async def test_a_purge_leaves_other_people_alone(memory: EndUserFixture) -> None:
    who = await alice(memory)
    bob = (await memory.end_user("bob")).id
    await memory.service.create_fact(memory.actor, who, text="Hers.")
    await memory.service.create_fact(memory.actor, bob, text="His.")

    await memory.service.purge(memory.actor, who)

    assert await memory.vectors.count(memory.organization_id, end_user_id=bob) == 1


async def test_a_purge_keeps_the_end_user_row(memory: EndUserFixture) -> None:
    """Erasure forgets what was *learned*; it does not rewrite the record that somebody
    made requests. The confirmation copy says so, so nobody presses it expecting the
    other thing."""
    who = await alice(memory)
    await memory.service.create_fact(memory.actor, who, text="Works in the EU.")

    await memory.service.purge(memory.actor, who)

    assert (await memory.service.get_end_user(memory.actor, who)).fact_count == 0


async def test_a_purge_can_take_the_transcripts_too(memory: EndUserFixture) -> None:
    organization = memory.organization
    who = await alice(memory)
    log = make_log_row(organization)
    log.end_user_id = who
    memory.database.request_logs[log.id] = log
    from app.db.models import Transcript

    memory.database.transcripts[log.id] = Transcript(
        request_log_id=log.id,
        created_at=log.created_at,
        organization_id=organization.id,
        request_body=[{"role": "user", "content": "my medical history"}],
    )

    result = await memory.service.purge(memory.actor, who, include_transcripts=True)

    assert result.transcripts == 1
    assert memory.database.transcripts == {}


async def test_a_purge_leaves_transcripts_alone_unless_asked(memory: EndUserFixture) -> None:
    """Deleting the bodies of every conversation is a larger act than forgetting what was
    learned from them, and it takes a second decision."""
    who = await alice(memory)
    log = make_log_row(memory.organization)
    log.end_user_id = who
    memory.database.request_logs[log.id] = log
    from app.db.models import Transcript

    memory.database.transcripts[log.id] = Transcript(
        request_log_id=log.id,
        created_at=log.created_at,
        organization_id=memory.organization.id,
        response_body="kept",
    )

    result = await memory.service.purge(memory.actor, who)

    assert result.transcripts == 0
    assert memory.database.transcripts != {}


async def test_a_purge_of_another_organizations_end_user_is_not_found(
    memory: EndUserFixture,
) -> None:
    other = build_end_users(
        make_organization(name="Globex", slug="globex"), database=memory.database
    )
    theirs = await other.end_user("alice")

    with pytest.raises(NotFound):
        await memory.service.purge(memory.actor, theirs.id)


async def test_an_unknown_end_user_is_not_found(memory: EndUserFixture) -> None:
    with pytest.raises(NotFound):
        await memory.service.get_end_user(memory.actor, uuid7())


async def test_a_fact_belongs_to_the_organization_of_the_person_it_is_about(
    memory: EndUserFixture,
) -> None:
    """SPEC §5.2. A superadmin can read across tenants for support, and a fact they write
    lands in the end user's organization rather than in no organization at all — which is
    what taking the tenant from the row rather than from the scope buys."""
    who = await alice(memory)
    platform = Actor(user_id=uuid7(), scope=TenantScope(role="superadmin", organization_id=None))

    fact = await memory.service.create_fact(platform, who, text="Added during support.")

    assert fact.organization_id == memory.organization_id
