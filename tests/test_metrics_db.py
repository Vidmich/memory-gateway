"""The request log against a real PostgreSQL: partitions, and the SQL nothing else checks.

Three things can only be true here.

**Partition routing.** ``request_logs`` is declaratively partitioned by day, and a row
lands in a partition or it does not. ``tableoid`` is what says which one, and there is no
way to fake that.

**``percentile_disc`` and ``date_bin``.** The in-memory repository reproduces both by
hand, which is worth exactly as much as the comparison against the real ones. The contract
list runs identically on both, so a divergence fails on one side.

**The flusher's cross-tenant insert.** It is the one statement in the system that
deliberately writes rows for several organizations at once, and it declares itself
unscoped. If that declaration were removed, the scope guard would refuse it here.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models.request_log import PARTITION_DAYS_AHEAD
from app.services.log_store import PostgresLogWriter
from app.services.metrics_store import PostgresMetricsRepository
from app.services.request_log import LogPolicy, RequestRecord
from tests.auth_support import make_organization
from tests.metrics_store_contract import CHECKS, Check, Fixture
from tests.monitoring_support import NOW, make_log_row, metrics_seed

pytestmark = pytest.mark.db


def organization(prefix: str) -> object:
    """A fresh organization. The slug is globally unique, so it cannot be a constant."""
    return make_organization(name=prefix.title(), slug=f"{prefix}-{uuid.uuid4().hex[:8]}")


@pytest.fixture
async def fixture(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Fixture]:
    """The same rows the in-memory half seeds, inserted for real."""
    acme = make_organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    globex = make_organization(name="Globex", slug=f"globex-{uuid.uuid4().hex[:8]}")
    db_session.add_all([acme, globex])
    await db_session.flush()

    seed = metrics_seed(acme, globex)
    # Before the logs: the throttled-caller query joins ``end_users``, and a foreign key
    # is a foreign key.
    db_session.add_all(list(seed.end_users))
    await db_session.flush()
    db_session.add_all(list(seed.logs))
    db_session.add_all(list(seed.transcripts))
    await db_session.flush()

    yield Fixture(
        repository=PostgresMetricsRepository(db_session_factory),
        acme=acme,
        globex=globex,
        acme_gateway_id=seed.acme_gateway_id,
        other_gateway_id=seed.other_gateway_id,
        globex_gateway_id=seed.globex_gateway_id,
        acme_log_id=seed.acme_log_id,
        bodiless_log_id=seed.bodiless_log_id,
        globex_log_id=seed.globex_log_id,
        noisy_end_user_id=seed.noisy_end_user_id,
        quiet_end_user_id=seed.quiet_end_user_id,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)


# ---------------------------------------------------------------------------
# partitions
# ---------------------------------------------------------------------------


async def partition_of(session: AsyncSession, log_id: uuid.UUID) -> str:
    row = await session.execute(
        text("SELECT tableoid::regclass::text FROM request_logs WHERE id = :id"),
        {"id": log_id},
    )
    return str(row.scalar_one())


async def test_a_row_lands_in_the_partition_for_its_day(db_session: AsyncSession) -> None:
    org = organization("part")
    db_session.add(org)
    await db_session.flush()

    row = make_log_row(org, created_at=NOW)  # type: ignore[arg-type]
    db_session.add(row)
    await db_session.flush()

    assert await partition_of(db_session, row.id) == f"request_logs_{NOW:%Y%m%d}"


async def test_two_days_land_in_two_partitions(db_session: AsyncSession) -> None:
    """The property retention depends on: dropping one day cannot take another with it."""
    org = organization("part")
    db_session.add(org)
    await db_session.flush()

    today = make_log_row(org, created_at=NOW)  # type: ignore[arg-type]
    tomorrow = make_log_row(org, created_at=NOW + timedelta(days=1))  # type: ignore[arg-type]
    db_session.add_all([today, tomorrow])
    await db_session.flush()

    first = await partition_of(db_session, today.id)
    second = await partition_of(db_session, tomorrow.id)

    assert first != second


async def test_a_row_beyond_the_runway_is_misfiled_rather_than_lost(
    db_session: AsyncSession,
) -> None:
    """The default partition earning its keep.

    An insert with no matching partition is an *error*, and an error on the logging path
    is a lost record. Past the runway the row lands in ``_default`` instead — visible,
    queryable, and task 17's problem rather than a gap in somebody's monitoring.
    """
    org = organization("part")
    db_session.add(org)
    await db_session.flush()

    far = make_log_row(org, created_at=NOW + timedelta(days=PARTITION_DAYS_AHEAD + 400))  # type: ignore[arg-type]
    db_session.add(far)
    await db_session.flush()

    assert await partition_of(db_session, far.id) == "request_logs_default"


async def test_transcripts_are_partitioned_the_same_way(db_session: AsyncSession) -> None:
    """Both tables carve the same days, so a retention drop takes the pair together."""
    count = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM pg_class "
                "WHERE relispartition AND relname LIKE 'transcripts\\_2%'"
            )
        )
    ).scalar_one()

    assert count >= PARTITION_DAYS_AHEAD


# ---------------------------------------------------------------------------
# the write path
# ---------------------------------------------------------------------------


async def test_the_flusher_writes_both_tables(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    org = organization("write")
    db_session.add(org)
    await db_session.flush()

    record = RequestRecord(
        organization_id=org.id,  # type: ignore[attr-defined]
        gateway_id=uuid7(),
        created_at=NOW,
        status_code=200,
        latency_total_ms=42,
        policy=LogPolicy(),
        request_body=[{"role": "user", "content": "hello"}],
        response_body="hi",
    )

    await PostgresLogWriter(db_session_factory).write([record])

    stored = (
        await db_session.execute(
            text("SELECT status_code, latency_total_ms FROM request_logs WHERE id = :id"),
            {"id": record.id},
        )
    ).one()
    body = (
        await db_session.execute(
            text("SELECT response_body FROM transcripts WHERE request_log_id = :id"),
            {"id": record.id},
        )
    ).scalar_one()

    assert tuple(stored) == (200, 42)
    assert body == "hi"


async def test_a_batch_may_span_organizations(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The one deliberately cross-tenant write in the system.

    A flush holds whatever the last half-second brought, from every organization on the
    process. It says so with :func:`app.db.scoping.unscoped`; without that declaration the
    guard refuses it, which is what makes the declaration mean something.
    """
    one, two = organization("one"), organization("two")
    db_session.add_all([one, two])
    await db_session.flush()

    records = [
        RequestRecord(
            organization_id=org.id,  # type: ignore[attr-defined]
            gateway_id=uuid7(),
            created_at=NOW,
            status_code=200,
        )
        for org in (one, two)
    ]

    await PostgresLogWriter(db_session_factory).write(records)

    count = (
        await db_session.execute(
            text("SELECT count(*) FROM request_logs WHERE id = ANY(:ids)"),
            {"ids": [record.id for record in records]},
        )
    ).scalar_one()

    assert count == 2


async def test_a_record_with_no_bodies_writes_no_transcript(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    org = organization("nobody")
    db_session.add(org)
    await db_session.flush()

    record = RequestRecord(
        organization_id=org.id,  # type: ignore[attr-defined]
        gateway_id=uuid7(),
        created_at=NOW,
        status_code=200,
    )

    await PostgresLogWriter(db_session_factory).write([record])

    count = (
        await db_session.execute(
            text("SELECT count(*) FROM transcripts WHERE request_log_id = :id"),
            {"id": record.id},
        )
    ).scalar_one()

    assert count == 0
