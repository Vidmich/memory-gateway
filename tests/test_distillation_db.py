"""The distillation store against a real PostgreSQL.

Three things that cannot be checked anywhere else.

**The join.** ``pending`` reads ``transcripts`` joined to ``request_logs`` on
``(id, created_at)``, because both tables are partitioned by day and the primary key is
only unique within a partition. A join written on the id alone works in the in-memory twin
and finds nothing here, which is exactly the kind of divergence a contract test exists to
catch — and the symptom in production would be memory that silently never gets written.

**The null session.** ``session_id = NULL`` is never true in SQL, so the "this thread has
no id" case has to be written as ``IS NULL``. Getting it wrong in the obvious direction
makes a job with no session match every conversation the person has ever had, which is a
transcript from one thread being distilled into facts attributed to another.

**The constraints.** A CHECK is only real if the server enforces it.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import DistillationRun, EndUser, MemoryFact
from app.services.distillation_store import (
    SUCCEEDED,
    PostgresDistillationStore,
    day_window,
    start_of_day,
)
from tests.distillation_store_contract import CHECKS, Check, Fixture, run_of
from tests.test_distillation_store_memory import build_fixture

pytestmark = pytest.mark.db


@pytest.fixture
async def fixture(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Fixture]:
    """The same rows the in-memory half seeds, inserted for real."""
    database, memory = build_fixture()

    for organization in database.organizations.values():
        db_session.add(organization)
    await db_session.flush()
    for end_user in database.end_users.values():
        db_session.add(end_user)
    for fact in database.memory_facts.values():
        db_session.add(fact)
    await db_session.flush()
    for log in database.request_logs.values():
        db_session.add(log)
    await db_session.flush()
    for transcript in database.transcripts.values():
        db_session.add(transcript)
    await db_session.flush()

    yield Fixture(
        store=PostgresDistillationStore(db_session_factory),
        acme=memory.acme,
        globex=memory.globex,
        acme_end_user=memory.acme_end_user,
        globex_end_user=memory.globex_end_user,
        acme_logs=memory.acme_logs,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


# ---------------------------------------------------------------------------
# what only a server can answer
# ---------------------------------------------------------------------------


async def test_an_unknown_outcome_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    db_session.add(
        DistillationRun(
            id=uuid7(),
            organization_id=fixture.acme.id,
            end_user_id=fixture.acme_end_user.id,
            outcome="probably",
        )
    )

    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_a_negative_count_is_refused_by_the_server(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The dispositions are what the health rates divide by. A negative one would make a
    rate that is neither wrong nor computable."""
    db_session.add(
        DistillationRun(
            id=uuid7(),
            organization_id=fixture.acme.id,
            end_user_id=fixture.acme_end_user.id,
            outcome=SUCCEEDED,
            inserted=-1,
        )
    )

    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_deleting_an_end_user_takes_their_runs_with_them(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """Unlike a request log, a run carries no traffic figures a chart would lose. It is
    about a person, and it goes when they do."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture))
        await transaction.commit()

    stored = await db_session.get(EndUser, fixture.acme_end_user.id)
    assert stored is not None
    await db_session.delete(stored)
    await db_session.flush()

    remaining = await db_session.execute(select(func.count()).select_from(DistillationRun))
    assert remaining.scalar() == 0


async def test_superseding_a_fact_records_which_one_replaced_it(
    db_session: AsyncSession, fixture: Fixture
) -> None:
    """The column task 13 adds, and the reason it is ``SET NULL`` rather than ``CASCADE``:
    deleting the replacement must not delete the history it replaced."""
    old = next(iter((await db_session.execute(select(MemoryFact))).scalars()))
    replacement = MemoryFact(
        id=uuid7(),
        organization_id=fixture.acme.id,
        end_user_id=fixture.acme_end_user.id,
        text="Works in Go.",
        kind="fact",
        confidence=1.0,
    )
    db_session.add(replacement)
    await db_session.flush()

    old.superseded_at = datetime.now(UTC)
    old.superseded_by_id = replacement.id
    await db_session.flush()

    await db_session.delete(replacement)
    await db_session.flush()
    await db_session.refresh(old)

    assert old.superseded_at is not None
    assert old.superseded_by_id is None


async def test_marking_a_transcript_carries_its_partition_key(fixture: Fixture) -> None:
    """A partitioned table's primary key is only unique within one partition, so an update
    by id alone has to probe every partition — and may match a row in the wrong day."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        pending = await transaction.pending(fixture.acme_end_user.id, "thread-1")
        marked = await transaction.mark_distilled(pending, at=datetime.now(UTC))
        await transaction.commit()

    assert marked == len(pending)


def test_the_daily_window_starts_at_utc_midnight() -> None:
    """The cap resets there, and the number on the Settings screen has to be the number the
    guard compares against — which means both read the same function."""
    midnight = start_of_day(datetime(2026, 9, 11, 23, 59, tzinfo=UTC))

    assert midnight == datetime(2026, 9, 11, tzinfo=UTC)


def test_the_health_window_covers_whole_days_up_to_the_next_midnight() -> None:
    start, end = day_window(7, now=datetime(2026, 9, 11, 12, 0, tzinfo=UTC))

    assert end - start == timedelta(days=7)
    assert end == datetime(2026, 9, 12, tzinfo=UTC)
