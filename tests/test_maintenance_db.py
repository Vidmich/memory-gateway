"""The maintenance SQL against a real PostgreSQL.

Four things can only be true here, and each of them is a place where the in-memory double
is a model rather than a copy.

**Partitions are read from the catalog.** ``pg_inherits`` is the truth about which days
exist; the memory store keeps a set, which is all the policy layer ever asks of it. Only
this file proves the two agree.

**A partition is created and dropped as DDL.** ``CREATE TABLE ... PARTITION OF`` and
``DROP TABLE`` are the whole reason these tables are partitioned, and neither has an
in-memory analogue worth the name.

**The per-gateway prune is a subquery across two partitioned tables.** It is the statement
with the most room to be quietly wrong — a join that fails to prune to one partition, a
bound that includes midnight twice — and it is exactly the statement whose cost the design
turns on.

**The scope guard sees these statements.** Every one of them declares itself unscoped with
a reason. If a declaration were removed, the guard would refuse it here and nowhere else.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import Gateway, Organization
from app.db.models.request_log import PARTITION_DAYS_AHEAD
from app.services.maintenance import RUNWAY_DAYS
from app.services.maintenance_store import (
    PostgresMaintenanceStore,
    PostgresMaintenanceTransaction,
    partition_name,
)
from tests.auth_support import make_organization
from tests.gateway_support import make_gateway_row

pytestmark = pytest.mark.db

#: Far enough back that no partition the task 07 migration created covers it, so the tests
#: below have to create their own — which is the point.
LONG_AGO = 400


@pytest.fixture
async def store(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> PostgresMaintenanceStore:
    return PostgresMaintenanceStore(db_session_factory)


@pytest.fixture
async def transaction(
    store: PostgresMaintenanceStore,
) -> AsyncIterator[PostgresMaintenanceTransaction]:
    async with store.begin() as open_transaction:
        # The concrete transaction, not the protocol: these tests reach for the session
        # to check the catalog and to write rows, which is the whole point of the file.
        assert isinstance(open_transaction, PostgresMaintenanceTransaction)
        yield open_transaction


def day(offset: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=offset)


async def test_the_runway_the_migration_created_is_visible_in_the_catalog(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """And it matches the constant the models declare, which is the pair that has to stay
    in step: a shorter runway in one of them sends every row to the default partition once
    the window closes."""
    days = await transaction.partition_days("request_logs")

    assert len(days) >= PARTITION_DAYS_AHEAD
    assert max(days) >= datetime.now(UTC).date() + timedelta(days=RUNWAY_DAYS - 1)


async def test_the_default_partition_is_not_mistaken_for_a_day(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """The safety net the migration leaves in place: an insert with no matching partition
    is an error, and an error on the logging path is a dropped record. Nothing here may
    drop it, and it must never appear in a list of days."""
    days = await transaction.partition_days("request_logs")

    assert all(value is not None for value in days)
    rows = await transaction._session.execute(
        text("SELECT to_regclass('request_logs_default') IS NOT NULL")
    )
    assert rows.scalar() is True


async def test_a_partition_can_be_created_and_dropped(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    target = day(LONG_AGO).date()
    assert target not in await transaction.partition_days("request_logs")

    await transaction.create_partition("request_logs", target)
    assert target in await transaction.partition_days("request_logs")

    await transaction.drop_partition("request_logs", target)
    assert target not in await transaction.partition_days("request_logs")


async def test_creating_a_partition_that_exists_is_not_an_error(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """Two replicas running the pass at once is a race worth winning quietly."""
    target = day(LONG_AGO + 1).date()
    await transaction.create_partition("request_logs", target)

    await transaction.create_partition("request_logs", target)

    assert target in await transaction.partition_days("request_logs")


async def test_dropping_a_partition_takes_its_rows_with_it(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """The whole reason for partitioning: retention is a ``DROP TABLE`` rather than a
    ``DELETE`` that rewrites a live table while the proxy is writing to it."""
    organization, gateway = await _seed(transaction)
    target = day(LONG_AGO).date()
    await transaction.create_partition("request_logs", target)
    await transaction.create_partition("transcripts", target)
    await _write(transaction, organization, gateway, at=day(LONG_AGO))

    assert await _count(transaction, "request_logs", gateway) == 1
    await transaction.drop_partition("transcripts", target)
    await transaction.drop_partition("request_logs", target)

    assert await _count(transaction, "request_logs", gateway) == 0


async def test_the_prune_removes_one_gateways_bodies_on_one_day(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    organization, gateway = await _seed(transaction)
    _, other = await _seed(transaction, organization=organization, slug="other")
    yesterday = day(1)
    await _write(transaction, organization, gateway, at=yesterday)
    await _write(transaction, organization, other, at=yesterday)

    pruned = await transaction.prune_bodies(gateway.id, yesterday.date())

    assert pruned.rows == 1
    assert pruned.bytes > 0
    assert await _count(transaction, "transcripts", gateway) == 0
    assert await _count(transaction, "transcripts", other) == 1
    # The metadata row is what SPEC §10.2 keeps for longer, and the prune must not touch it.
    assert await _count(transaction, "request_logs", gateway) == 1


async def test_the_prune_is_bounded_to_the_day_it_names(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """The bounds are half-open on both tables. A day that included midnight twice would
    delete a neighbour's rows, and the neighbour is a day somebody is still entitled to."""
    organization, gateway = await _seed(transaction)
    await _write(transaction, organization, gateway, at=day(1))
    await _write(transaction, organization, gateway, at=day(2))

    await transaction.prune_bodies(gateway.id, day(1).date())

    assert await _count(transaction, "transcripts", gateway) == 1


async def test_pruning_metadata_takes_the_bodies_with_it(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """A metadata row is what makes a transcript reachable, so deleting one without the
    other would leave bodies nothing can read and nothing can remove."""
    organization, gateway = await _seed(transaction)
    await _write(transaction, organization, gateway, at=day(1))

    pruned = await transaction.prune_metadata(gateway.id, day(1).date())

    assert pruned.rows == 1
    assert await _count(transaction, "request_logs", gateway) == 0
    assert await _count(transaction, "transcripts", gateway) == 0


async def test_pruning_a_day_with_nothing_on_it_is_free(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """Which is what makes the whole pass idempotent, and therefore resumable."""
    _, gateway = await _seed(transaction)

    pruned = await transaction.prune_bodies(gateway.id, day(3).date())

    assert pruned.rows == 0
    assert pruned.bytes == 0


async def test_a_gateways_retention_is_read_from_its_stored_configuration(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    organization, gateway = await _seed(
        transaction, logging_config={"retention_days": 3, "metadata_retention_days": 90}
    )

    found = next(
        entry for entry in await transaction.retentions() if entry.gateway_id == gateway.id
    )

    assert (found.body_days, found.metadata_days) == (3, 90)
    assert found.organization_id == organization.id


async def test_an_unconfigured_gateway_gets_the_documented_defaults(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """A row storing ``{}`` has to load as SPEC §10.2's 30 and 365 — the defaults *are*
    the documentation, and a job that read zero from an empty blob would delete everything
    on its first night."""
    _, gateway = await _seed(transaction, logging_config={})

    found = next(
        entry for entry in await transaction.retentions() if entry.gateway_id == gateway.id
    )

    assert (found.body_days, found.metadata_days) == (30, 365)


async def test_a_maintenance_run_records_its_cursor_and_its_report(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    run = await transaction.open_run("retention")

    await transaction.save_run(run.id, cursor={"floors": {"a": "2026-01-01"}})
    adopted = await transaction.unfinished("retention")
    assert adopted is not None
    assert adopted.id == run.id

    await transaction.save_run(run.id, report={"rows_removed": 4}, status="succeeded")

    assert await transaction.unfinished("retention") is None
    finished = (await transaction.recent_runs(limit=1))[0]
    assert finished.report["rows_removed"] == 4
    assert finished.cursor["floors"]["a"] == "2026-01-01"
    assert finished.finished_at is not None


async def test_the_partition_names_the_store_builds_are_the_ones_postgresql_has(
    transaction: PostgresMaintenanceTransaction,
) -> None:
    """The two halves of the same string: the DDL writes it and the catalog reads it back,
    and a mismatch would make every drop a silent no-op."""
    target = day(LONG_AGO + 2).date()
    await transaction.create_partition("transcripts", target)

    rows = await transaction._session.execute(
        text("SELECT to_regclass(:name) IS NOT NULL"),
        {"name": partition_name("transcripts", target)},
    )

    assert rows.scalar() is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed(
    transaction: PostgresMaintenanceTransaction,
    *,
    organization: Organization | None = None,
    slug: str = "chat",
    logging_config: dict[str, object] | None = None,
) -> tuple[Organization, Gateway]:
    """An organization and a gateway, written straight through the session.

    Through the ORM rather than the stores, because what these tests are about is the SQL
    below them: building the rows through four services would make a failure here read as
    a failure of whichever service changed last.
    """
    session = transaction._session
    row = organization or make_organization(slug=f"acme-{uuid7().hex[:8]}")
    if organization is None:
        session.add(row)
        await session.flush()
    gateway = make_gateway_row(
        row,
        slug=f"{slug}-{uuid7().hex[:8]}",
        logging_config=logging_config
        if logging_config is not None
        else {"retention_days": 30, "metadata_retention_days": 365},
    )
    session.add(gateway)
    await session.flush()
    return row, gateway


async def _write(
    transaction: PostgresMaintenanceTransaction,
    organization: Organization,
    gateway: Gateway,
    *,
    at: datetime,
) -> uuid.UUID:
    session = transaction._session
    identifier = uuid7()
    await session.execute(
        text(
            "INSERT INTO request_logs (id, created_at, organization_id, gateway_id, status_code)"
            " VALUES (:id, :at, :organization, :gateway, 200)"
        ),
        {
            "id": identifier,
            "at": at,
            "organization": organization.id,
            "gateway": gateway.id,
        },
    )
    await session.execute(
        text(
            "INSERT INTO transcripts"
            " (request_log_id, created_at, organization_id, response_body)"
            " VALUES (:id, :at, :organization, :body)"
        ),
        {
            "id": identifier,
            "at": at,
            "organization": organization.id,
            "body": "a response long enough to measure",
        },
    )
    return identifier


async def _count(transaction: PostgresMaintenanceTransaction, table: str, gateway: Gateway) -> int:
    session = transaction._session
    if table == "request_logs":
        statement = text("SELECT count(*) FROM request_logs WHERE gateway_id = :gateway")
    else:
        statement = text(
            "SELECT count(*) FROM transcripts t"
            " JOIN request_logs r ON r.id = t.request_log_id AND r.created_at = t.created_at"
            " WHERE r.gateway_id = :gateway"
        )
    rows = await session.execute(statement, {"gateway": gateway.id})
    return int(rows.scalar() or 0)
