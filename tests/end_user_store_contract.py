"""One set of assertions for the end-user store, run against memory and PostgreSQL.

Same shape and same reasons as ``tests/connector_store_contract.py``, and two checks here
earn the file on their own.

:func:`touching_the_same_identity_twice_produces_one_row` is an ``ON CONFLICT`` upsert in
PostgreSQL and a dictionary lookup in memory — the same pair that made ``claim_document``
worth a contract, and with the same consequence if they drift: two rows for one person,
whose memories then diverge depending on which one a request happened to resolve.

:func:`recall_never_reads_a_superseded_or_expired_fact` is a SQL ``WHERE`` on one side and
a Python predicate on the other. It is the one rule conversation memory cannot get wrong —
a customer who retracts a fact has to see it stop being used — so it is asserted against
both implementations rather than against whichever one a test happened to build.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.tenancy import TenantScope
from app.db.models import EndUser, Organization
from app.services.end_user_store import EndUserStore, FactDraft


@dataclass(frozen=True)
class Fixture:
    store: EndUserStore
    acme: Organization
    globex: Organization
    acme_end_user: EndUser
    globex_end_user: EndUser

    def scope(self, organization: Organization) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=organization.id)

    @property
    def acme_scope(self) -> TenantScope:
        return self.scope(self.acme)

    @property
    def globex_scope(self) -> TenantScope:
        return self.scope(self.globex)


Check = Callable[[Fixture], Awaitable[None]]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


def draft(text: str = "Prefers Python.", **kwargs: object) -> FactDraft:
    return FactDraft(
        text=text,
        kind=str(kwargs.get("kind", "preference")),
        confidence=float(kwargs.get("confidence", 1.0)),  # type: ignore[arg-type]
        expires_at=kwargs.get("expires_at"),  # type: ignore[arg-type]
    )


def now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


@check
async def touching_a_new_identity_creates_a_row(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        created = await tx.touch("brand-new")
        await tx.commit()

    assert created.external_id == "brand-new"
    assert created.organization_id == fixture.acme.id
    assert created.request_count == 0


@check
async def touching_the_same_identity_twice_produces_one_row(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        first = await tx.touch("repeat")
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        second = await tx.touch("repeat")
        await tx.commit()

    assert first.id == second.id


@check
async def the_same_external_id_in_two_organizations_is_two_people(fixture: Fixture) -> None:
    """The ordinary case, not a collision: every customer has a user called ``alice``."""
    async with fixture.store.begin(fixture.acme_scope) as tx:
        mine = await tx.touch("alice")
        await tx.commit()
    async with fixture.store.begin(fixture.globex_scope) as tx:
        theirs = await tx.touch("alice")
        await tx.commit()

    assert mine.id != theirs.id


@check
async def an_end_user_from_another_organization_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        assert await tx.end_user(fixture.globex_end_user.id) is None


@check
async def a_list_never_includes_another_organization(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        rows = await tx.end_users(after=None, limit=50)

    assert fixture.globex_end_user.id not in {row.id for row in rows}
    assert fixture.acme_end_user.id in {row.id for row in rows}


@check
async def search_matches_part_of_an_external_id(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        await tx.touch("customer-4471@example.com")
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        found = await tx.end_users(after=None, limit=50, search="4471")

    assert [row.external_id for row in found] == ["customer-4471@example.com"]


@check
async def a_wildcard_in_a_search_matches_nothing_rather_than_everything(
    fixture: Fixture,
) -> None:
    """A search box that returns the whole table for ``%`` is a search box that lies."""
    async with fixture.store.begin(fixture.acme_scope) as tx:
        found = await tx.end_users(after=None, limit=50, search="%")

    assert found == []


@check
async def counters_are_added_to_rather_than_replaced(fixture: Fixture) -> None:
    stamp = now()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        updated = await tx.bump({fixture.acme_end_user.id: 3}, seen_at=stamp)
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        await tx.bump({fixture.acme_end_user.id: 2}, seen_at=stamp)
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        row = await tx.end_user(fixture.acme_end_user.id)

    assert updated == 1
    assert row is not None
    assert row.request_count == 5


@check
async def a_counter_flush_never_moves_last_seen_backwards(fixture: Fixture) -> None:
    """Two replicas flushing out of order must not rewind the column somebody sorts by."""
    later = now()
    earlier = later - timedelta(hours=1)
    async with fixture.store.begin(fixture.acme_scope) as tx:
        await tx.bump({fixture.acme_end_user.id: 1}, seen_at=later)
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        await tx.bump({fixture.acme_end_user.id: 1}, seen_at=earlier)
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        row = await tx.end_user(fixture.acme_end_user.id)

    assert row is not None
    assert row.last_seen_at >= later - timedelta(seconds=1)


@check
async def counters_for_another_organization_are_ignored(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        updated = await tx.bump({fixture.globex_end_user.id: 9}, seen_at=now())
        await tx.commit()

    assert updated == 0


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------


@check
async def a_fact_is_stored_and_read_back(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        created = await tx.add_fact(fixture.acme_end_user, draft("Works in the EU."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        found = await tx.fact(created.id)

    assert found is not None
    assert found.text == "Works in the EU."
    assert found.organization_id == fixture.acme.id


@check
async def a_fact_from_another_organization_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.globex_scope) as tx:
        theirs = await tx.add_fact(fixture.globex_end_user, draft("Globex only."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        assert await tx.fact(theirs.id) is None


@check
async def recall_never_reads_a_superseded_or_expired_fact(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        live = await tx.add_fact(fixture.acme_end_user, draft("Still true."))
        retracted = await tx.add_fact(fixture.acme_end_user, draft("No longer true."))
        stale = await tx.add_fact(
            fixture.acme_end_user, draft("Was true until yesterday.", expires_at=None)
        )
        retracted.superseded_at = now()
        stale.expires_at = now() - timedelta(days=1)
        await tx.commit()

    ids = [live.id, retracted.id, stale.id]
    async with fixture.store.begin(fixture.acme_scope) as tx:
        found = await tx.live_facts(fixture.acme_end_user.id, ids, now=now())

    assert [row.id for row in found] == [live.id]


@check
async def live_facts_will_not_cross_to_another_end_user(fixture: Fixture) -> None:
    """The ids come from a vector store, so a filter being wrong there must not be able
    to become a disclosure here."""
    async with fixture.store.begin(fixture.globex_scope) as tx:
        theirs = await tx.add_fact(fixture.globex_end_user, draft("Globex only."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        found = await tx.live_facts(fixture.acme_end_user.id, [theirs.id], now=now())

    assert found == []


@check
async def the_always_include_set_is_the_most_recent_confident_facts(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        old = await tx.add_fact(fixture.acme_end_user, draft("Learned long ago."))
        unsure = await tx.add_fact(fixture.acme_end_user, draft("Maybe.", confidence=0.4))
        fresh = await tx.add_fact(fixture.acme_end_user, draft("Learned today."))
        old.last_seen_at = now() - timedelta(days=400)
        unsure.last_seen_at = now()
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        found = await tx.recent_facts(
            fixture.acme_end_user.id, limit=2, min_confidence=0.8, now=now()
        )

    ids = [row.id for row in found]
    assert fresh.id in ids
    assert unsure.id not in ids
    assert ids[0] == fresh.id


@check
async def counting_facts_ignores_the_retracted_ones(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        await tx.add_fact(fixture.acme_end_user, draft("One."))
        gone = await tx.add_fact(fixture.acme_end_user, draft("Two."))
        gone.superseded_at = now()
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        live = await tx.count_facts(fixture.acme_end_user.id)
        every = await tx.count_facts(fixture.acme_end_user.id, live_only=False)

    assert (live, every) == (1, 2)


@check
async def listing_facts_shows_the_retracted_ones_unless_asked_otherwise(
    fixture: Fixture,
) -> None:
    """ "Why did it say that last month" is answered by the fact that has since been
    replaced, so the browser has to be able to see it."""
    async with fixture.store.begin(fixture.acme_scope) as tx:
        gone = await tx.add_fact(fixture.acme_end_user, draft("Retracted."))
        gone.superseded_at = now()
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        everything = await tx.facts(fixture.acme_end_user.id, after=None, limit=50)
        only_live = await tx.facts(fixture.acme_end_user.id, after=None, limit=50, live_only=True)

    assert gone.id in {row.id for row in everything}
    assert gone.id not in {row.id for row in only_live}


@check
async def fact_counts_answer_for_a_whole_page_at_once(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        other = await tx.touch("second-person")
        await tx.add_fact(fixture.acme_end_user, draft("One."))
        await tx.add_fact(fixture.acme_end_user, draft("Two."))
        await tx.add_fact(other, draft("Theirs."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        counts = await tx.fact_counts([fixture.acme_end_user.id, other.id])

    assert counts[fixture.acme_end_user.id] == 2
    assert counts[other.id] == 1


@check
async def deleting_every_fact_for_one_end_user_leaves_the_others(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        other = await tx.touch("bystander")
        await tx.add_fact(fixture.acme_end_user, draft("Mine."))
        await tx.add_fact(fixture.acme_end_user, draft("Also mine."))
        kept = await tx.add_fact(other, draft("Not mine."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        removed = await tx.delete_facts_of(fixture.acme_end_user.id)
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        assert removed == 2
        assert await tx.count_facts(fixture.acme_end_user.id, live_only=False) == 0
        assert await tx.fact(kept.id) is not None


@check
async def a_purge_from_the_wrong_organization_removes_nothing(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.globex_scope) as tx:
        theirs = await tx.add_fact(fixture.globex_end_user, draft("Globex only."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        removed = await tx.delete_facts_of(fixture.globex_end_user.id)
        await tx.commit()
    async with fixture.store.begin(fixture.globex_scope) as tx:
        assert removed == 0
        assert await tx.fact(theirs.id) is not None


@check
async def facts_page_by_id(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as tx:
        first = await tx.add_fact(fixture.acme_end_user, draft("One."))
        second = await tx.add_fact(fixture.acme_end_user, draft("Two."))
        await tx.commit()
    async with fixture.store.begin(fixture.acme_scope) as tx:
        page = await tx.facts(fixture.acme_end_user.id, after=second.id, limit=50)

    assert [row.id for row in page] == [first.id]


__all__ = ["CHECKS", "Check", "Fixture", "check", "draft"]
