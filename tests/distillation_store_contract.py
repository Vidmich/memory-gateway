"""One set of assertions for the distillation store, run against memory and PostgreSQL.

Two of these checks earn the file on their own.

:func:`only_this_threads_undistilled_transcripts_are_read` is a three-way join with a
``distilled_at IS NULL`` predicate on one side and a dictionary walk on the other. It
decides what a model is shown, so an implementation that quietly widened it — matching
every session, or re-reading rows a previous pass already covered — would send one person's
whole history to an extractor on every turn, and the only visible symptom would be a bill.

:func:`a_null_session_matches_only_the_rows_that_have_none` is the SQL detail that has no
Python equivalent: ``session_id = NULL`` is never true, so the PostgreSQL side has to say
``IS NULL`` and the memory side has to compare to ``None``. Getting it wrong in the obvious
direction makes a job with no session match *every* conversation the person has ever had.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.tenancy import TenantScope
from app.db.models import EndUser, Organization
from app.services.distillation_store import (
    FAILED,
    SKIPPED,
    SUCCEEDED,
    DistillationStore,
    RunRecord,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Fixture:
    store: DistillationStore
    acme: Organization
    globex: Organization
    acme_end_user: EndUser
    globex_end_user: EndUser
    #: ``(log_id, session_id, distilled)`` for the rows the fixture seeded, in the order
    #: they were logged. Lets a check name a transcript without re-deriving its id.
    acme_logs: tuple[tuple[uuid.UUID, str | None, bool], ...] = ()

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


def run_of(fixture: Fixture, **overrides: object) -> RunRecord:
    values: dict[str, object] = {
        "organization_id": fixture.acme.id,
        "end_user_id": fixture.acme_end_user.id,
        "session_id": "thread-1",
        "outcome": SUCCEEDED,
    }
    values.update(overrides)
    return RunRecord(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reading transcripts
# ---------------------------------------------------------------------------


@check
async def only_this_threads_undistilled_transcripts_are_read(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-1")

    wanted = {
        log_id
        for log_id, session, distilled in fixture.acme_logs
        if session == "thread-1" and not distilled
    }
    assert {entry.log_id for entry in pending} == wanted


@check
async def pending_transcripts_come_back_oldest_first(fixture: Fixture) -> None:
    """The exchange is a conversation. Out of order it is a different conversation, and
    "actually I have moved to Go" read before "I work in Rust" reverses a supersession."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-1")

    stamps = [entry.created_at for entry in pending]
    assert stamps == sorted(stamps)


@check
async def a_null_session_matches_only_the_rows_that_have_none(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, None)

    wanted = {
        log_id
        for log_id, session, distilled in fixture.acme_logs
        if session is None and not distilled
    }
    assert {entry.log_id for entry in pending} == wanted
    assert wanted  # otherwise this check passes vacuously


@check
async def a_failed_request_is_not_offered_for_distillation(fixture: Fixture) -> None:
    """A 502 has no answer and usually a question nobody really asked."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-errors")

    assert pending == []


@check
async def another_organizations_transcripts_are_invisible(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.globex_end_user.id, "thread-1")

    assert pending == []


@check
async def the_bodies_come_back_with_the_row(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-1")

    assert pending[0].request_body == [{"role": "user", "content": "I work in Rust."}]
    assert pending[0].response_body == "Noted."


@check
async def marking_a_transcript_distilled_takes_it_out_of_the_next_pass(
    fixture: Fixture,
) -> None:
    """``distilled_at`` is the idempotency key: what makes a backfill safe to run twice
    and a duplicate job a no-op."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-1")
        marked = await transaction.mark_distilled(pending, at=datetime.now(UTC))
        await transaction.commit()

    assert marked == len(pending)
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.pending(fixture.acme_end_user.id, "thread-1") == []


@check
async def a_pass_reads_at_most_its_limit(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-1", limit=1)

    assert len(pending) == 1


# ---------------------------------------------------------------------------
# the backfill's worklist
# ---------------------------------------------------------------------------


@check
async def pending_sessions_group_by_thread(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        sessions = await transaction.pending_sessions(
            start=NOW - timedelta(days=30), end=NOW + timedelta(days=1), limit=50
        )

    sessions_by_id = {session.session_id for session in sessions}
    assert "thread-1" in sessions_by_id
    assert None in sessions_by_id
    assert "thread-errors" not in sessions_by_id


@check
async def pending_sessions_can_be_narrowed_to_one_person(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        sessions = await transaction.pending_sessions(
            start=NOW - timedelta(days=30),
            end=NOW + timedelta(days=1),
            limit=50,
            end_user_id=fixture.acme_end_user.id,
        )

    assert {session.end_user_id for session in sessions} == {fixture.acme_end_user.id}


@check
async def a_window_that_excludes_everything_finds_nothing(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        sessions = await transaction.pending_sessions(
            start=NOW + timedelta(days=10), end=NOW + timedelta(days=20), limit=50
        )

    assert sessions == []


@check
async def another_organizations_sessions_are_not_in_the_worklist(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        sessions = await transaction.pending_sessions(
            start=NOW - timedelta(days=30), end=NOW + timedelta(days=1), limit=50
        )

    assert {session.organization_id for session in sessions} == {fixture.acme.id}


# ---------------------------------------------------------------------------
# runs, caps and health
# ---------------------------------------------------------------------------


@check
async def a_run_is_recorded_and_counted(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture, inserted=2))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        counted = await transaction.calls_since(datetime.now(UTC) - timedelta(hours=1))

    assert counted == 1


@check
async def a_skipped_run_is_not_a_call(fixture: Fixture) -> None:
    """Refusing to call a model is not calling one. A cap that counted its own refusals
    would latch on for the rest of the day."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture, outcome=SKIPPED, reason="daily_cap_reached"))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.calls_since(datetime.now(UTC) - timedelta(hours=1)) == 0


@check
async def a_failed_run_is_a_call(fixture: Fixture) -> None:
    """It reached the provider and was billed for whatever it consumed."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture, outcome=FAILED, reason="timed out"))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.calls_since(datetime.now(UTC) - timedelta(hours=1)) == 1


@check
async def calls_can_be_counted_for_one_person(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        mine = await transaction.calls_since(
            datetime.now(UTC) - timedelta(hours=1), end_user_id=fixture.acme_end_user.id
        )
        theirs = await transaction.calls_since(
            datetime.now(UTC) - timedelta(hours=1), end_user_id=fixture.globex_end_user.id
        )

    assert (mine, theirs) == (1, 0)


@check
async def another_organizations_runs_are_not_counted(fixture: Fixture) -> None:
    """The cap is one organization's spend, and so is the chart."""
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        await transaction.record(
            run_of(
                fixture,
                organization_id=fixture.globex.id,
                end_user_id=fixture.globex_end_user.id,
            )
        )
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.calls_since(datetime.now(UTC) - timedelta(hours=1)) == 0


@check
async def health_reports_the_dispositions_and_the_rates(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture, candidates=4, inserted=1, deduped=2, superseded=1))
        await transaction.record(run_of(fixture, outcome=FAILED, reason="timed out"))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(days=1)
        )

    assert health.runs == 2
    assert health.failures == 1
    assert health.written == 1
    assert health.failure_rate == 0.5
    assert health.dedupe_rate == 0.5
    assert health.supersession_rate == 0.25


@check
async def health_counts_live_facts_and_the_people_they_are_about(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(days=1)
        )

    assert health.facts == 1
    assert health.end_users_with_facts == 1
    assert health.average_facts_per_end_user == 1.0


@check
async def health_ignores_runs_outside_the_window(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture, inserted=5))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=30), end=datetime.now(UTC) - timedelta(days=1)
        )

    assert health.runs == 0
    assert health.written == 0


@check
async def health_over_nothing_is_zero_rather_than_a_division_by_zero(
    fixture: Fixture,
) -> None:
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(days=1)
        )

    assert health.failure_rate == 0.0
    assert health.dedupe_rate == 0.0
    assert health.supersession_rate == 0.0
    assert health.average_facts_per_end_user == 0.0


@check
async def health_buckets_runs_by_day(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture, inserted=3))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(days=1)
        )

    assert len(health.days) == 1
    assert health.days[0].written == 3
