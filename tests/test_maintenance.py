"""Partition runway, retention, and the expired-fact purge.

The acceptance criteria in task 17 are mostly about *exactness*: two gateways on one
partition each honoured to the day, a runway that never silently runs out, a fact that
disappears from both stores rather than one. Each of those is a statement about a
boundary, so the tests here mostly set a clock and a configuration and assert which side
of the line a row lands on.

The planning functions are tested directly rather than through a job, because "which days
should exist" has an exact answer and driving it through a store would make a failure read
as a mystery about the store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.ids import uuid7
from app.db.models import Gateway, MemoryFact, RequestLog, Transcript
from app.schemas.platform import PlatformSettings
from app.services.maintenance import (
    LOW_RUNWAY_DAYS,
    RUNWAY_DAYS,
    days_ahead,
    expired_days,
    missing_days,
)
from app.services.maintenance_store import RunState, create_partition_sql, day_of
from tests.auth_support import make_organization
from tests.gateway_support import make_gateway_row
from tests.platform_support import PlatformFixture, build_platform

TODAY = date(2026, 9, 14)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_a_fresh_deployment_wants_a_month_of_runway_and_yesterday() -> None:
    """Yesterday as well as today: a worker with a slow clock writes a row dated a minute
    ago, and a row with no partition is an insert that fails."""
    wanted = missing_days((), today=TODAY)

    assert wanted[0] == TODAY - timedelta(days=1)
    assert wanted[-1] == TODAY + timedelta(days=RUNWAY_DAYS)
    assert len(wanted) == RUNWAY_DAYS + 2


def test_days_that_already_exist_are_not_created_again() -> None:
    existing = [TODAY + timedelta(days=offset) for offset in range(-1, 10)]

    assert missing_days(existing, today=TODAY) == [
        TODAY + timedelta(days=offset) for offset in range(10, RUNWAY_DAYS + 1)
    ]


def test_runway_counts_consecutive_days_not_partitions() -> None:
    """A deployment with today and a day next month has one day of runway, not two.

    The gap is where inserts start failing, and a count of rows in ``pg_inherits`` would
    report the reassuring number right up until midnight.
    """
    scattered = [TODAY, TODAY + timedelta(days=30)]

    assert days_ahead(scattered, today=TODAY) == 1


def test_runway_of_zero_is_what_the_alert_is_written_against() -> None:
    assert days_ahead([TODAY - timedelta(days=1)], today=TODAY) == 0


def test_a_partition_is_expired_only_once_it_is_older_than_the_longest_window() -> None:
    existing = [TODAY - timedelta(days=offset) for offset in range(0, 100)]

    expired = expired_days(existing, today=TODAY, keep_days=30)

    assert max(expired) == TODAY - timedelta(days=31)
    assert TODAY - timedelta(days=30) not in expired


def test_the_partition_ddl_pins_utc_rather_than_the_session_timezone() -> None:
    """A bare date literal is read in the session's ``TimeZone``, so the same statement run
    by two operators would carve the day differently and rows near midnight would land
    either side of the boundary."""
    statement = create_partition_sql("request_logs", TODAY)

    assert "request_logs_20260914" in statement
    assert "'2026-09-14 00:00:00+00'" in statement
    assert "'2026-09-15 00:00:00+00'" in statement


def test_a_partition_name_round_trips_and_the_default_one_does_not() -> None:
    assert day_of("request_logs_20260914") == TODAY
    # The safety net the task 07 migration leaves in place. Nothing here may drop it, and
    # returning `None` is what keeps it out of every list this module builds.
    assert day_of("request_logs_default") is None


# ---------------------------------------------------------------------------
# the runway job
# ---------------------------------------------------------------------------


@pytest.fixture
def platform() -> PlatformFixture:
    return build_platform()


async def test_the_partition_job_creates_the_missing_days(platform: PlatformFixture) -> None:
    report = await platform.partitions.ensure()

    for table in ("request_logs", "transcripts"):
        assert len(report.created[table]) == RUNWAY_DAYS + 2
        assert len(platform.store.partitions[table]) == RUNWAY_DAYS + 2


async def test_running_the_partition_job_twice_creates_nothing_the_second_time(
    platform: PlatformFixture,
) -> None:
    await platform.partitions.ensure()

    assert (await platform.partitions.ensure()).created == {}


async def test_a_low_runway_is_reported_as_low(platform: PlatformFixture) -> None:
    today = datetime.now(UTC).date()
    platform.partition_days("request_logs", [today + timedelta(days=offset) for offset in range(3)])
    platform.partition_days("transcripts", [today + timedelta(days=offset) for offset in range(3)])

    runway = await platform.partitions.runway()

    assert [entry.days_ahead for entry in runway] == [3, 3]
    assert all(entry.low for entry in runway)
    assert all(entry.threshold == LOW_RUNWAY_DAYS for entry in runway)


async def test_a_full_runway_is_not_low(platform: PlatformFixture) -> None:
    await platform.partitions.ensure()

    assert not any(entry.low for entry in await platform.partitions.runway())


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


def seed_gateway(
    platform: PlatformFixture,
    organization_id: uuid.UUID,
    *,
    name: str,
    body_days: int,
    metadata_days: int,
) -> Gateway:
    organization = make_organization()
    organization.id = organization_id
    gateway = make_gateway_row(
        organization,
        slug=name,
        name=name,
        logging_config={
            "retention_days": body_days,
            "metadata_retention_days": metadata_days,
        },
    )
    platform.db.add_gateway(gateway)
    return gateway


def seed_request(
    platform: PlatformFixture, gateway: Gateway, *, days_ago: int, body: str = "hello"
) -> uuid.UUID:
    """One request with its transcript, on the day it happened.

    Both rows carry the same ``created_at`` deliberately — it is the partition key on both
    tables, and a transcript a moment either side of midnight from its metadata is exactly
    the pair retention would take one of and leave the other.
    """
    at = datetime.now(UTC) - timedelta(days=days_ago)
    identifier = uuid7()
    platform.db.request_logs[identifier] = RequestLog(
        id=identifier,
        created_at=at,
        organization_id=gateway.organization_id,
        gateway_id=gateway.id,
        status_code=200,
    )
    platform.db.transcripts[identifier] = Transcript(
        request_log_id=identifier,
        created_at=at,
        organization_id=gateway.organization_id,
        request_body=[{"role": "user", "content": body}],
        response_body=body,
    )
    return identifier


async def test_bodies_past_the_window_go_and_the_metadata_row_stays(
    platform: PlatformFixture,
) -> None:
    """SPEC §10.2's promise, at its simplest: bodies are hard-deleted after
    ``retention_days`` and the row the monitoring screens read survives."""
    organization = uuid7()
    gateway = seed_gateway(platform, organization, name="chat", body_days=7, metadata_days=365)
    platform.with_partitions_around()
    old = seed_request(platform, gateway, days_ago=10)
    recent = seed_request(platform, gateway, days_ago=2)

    await platform.retention.run()

    assert old not in platform.db.transcripts
    assert old in platform.db.request_logs
    assert recent in platform.db.transcripts


async def test_two_gateways_on_one_partition_are_each_honoured_exactly(
    platform: PlatformFixture,
) -> None:
    """The acceptance criterion that makes the two-stage design necessary.

    ``retention_days`` is per gateway and a partition is global, so the same day holds
    rows with different claims on it. A pass that worked a whole day at a time would have
    to pick one of the two numbers.
    """
    organization = uuid7()
    strict = seed_gateway(platform, organization, name="strict", body_days=3, metadata_days=365)
    lenient = seed_gateway(platform, organization, name="lenient", body_days=30, metadata_days=365)
    platform.with_partitions_around()
    strict_old = seed_request(platform, strict, days_ago=5)
    lenient_old = seed_request(platform, lenient, days_ago=5)

    await platform.retention.run()

    assert strict_old not in platform.db.transcripts
    assert lenient_old in platform.db.transcripts


async def test_metadata_goes_when_its_own_window_expires(platform: PlatformFixture) -> None:
    organization = uuid7()
    gateway = seed_gateway(platform, organization, name="chat", body_days=1, metadata_days=10)
    platform.with_partitions_around()
    ancient = seed_request(platform, gateway, days_ago=20)
    kept = seed_request(platform, gateway, days_ago=5)

    await platform.retention.run()

    assert ancient not in platform.db.request_logs
    assert kept in platform.db.request_logs
    assert kept not in platform.db.transcripts


async def test_a_whole_partition_goes_once_nobody_can_claim_it(
    platform: PlatformFixture,
) -> None:
    """The cheap case, and the reason the tables are partitioned at all: past the longest
    metadata window any gateway has, the day is a ``DROP TABLE`` rather than a scan."""
    organization = uuid7()
    seed_gateway(platform, organization, name="chat", body_days=5, metadata_days=30)
    today = datetime.now(UTC).date()
    platform.with_partitions_around(before=60)

    report = await platform.retention.run()

    dropped = {date.fromisoformat(day) for day in report.partitions.dropped["request_logs"]}
    assert today - timedelta(days=45) in dropped
    assert today - timedelta(days=20) not in dropped


async def test_the_report_says_what_each_gateway_gave_up(platform: PlatformFixture) -> None:
    organization = uuid7()
    gateway = seed_gateway(platform, organization, name="chat", body_days=1, metadata_days=365)
    platform.with_partitions_around()
    for offset in (3, 4, 5):
        seed_request(platform, gateway, days_ago=offset, body="a longer body to measure")

    report = await platform.retention.run()

    entry = next(row for row in report.gateways if row.gateway_id == gateway.id)
    assert entry.bodies.rows == 3
    assert entry.name == "chat"
    assert report.bytes_reclaimed > 0
    assert report.as_json()["gateways"][0]["bodies_removed"] == 3


async def test_a_second_pass_the_same_night_removes_nothing_more(
    platform: PlatformFixture,
) -> None:
    """Idempotence, which is what makes the whole pass safe to resume: every unit deletes
    by predicate, so running it twice deletes nothing the second time."""
    organization = uuid7()
    gateway = seed_gateway(platform, organization, name="chat", body_days=1, metadata_days=365)
    platform.with_partitions_around()
    seed_request(platform, gateway, days_ago=5)
    await platform.retention.run()

    second = await platform.retention.run()

    assert second.bodies_removed == 0
    assert second.rows_removed == 0


async def test_the_floor_advances_so_a_later_pass_does_not_rewalk_a_year(
    platform: PlatformFixture,
) -> None:
    """The cursor is an optimisation, and this is the property it buys.

    Losing it would cost time and nothing else — every unit is idempotent — but a steady
    state of one gateway times three hundred and sixty-five empty deletes a night is the
    kind of cost that eventually shows up as a nightly latency spike.
    """
    organization = uuid7()
    gateway = seed_gateway(platform, organization, name="chat", body_days=1, metadata_days=30)
    platform.with_partitions_around()
    await platform.retention.run()

    runs = await _runs(platform, "retention")
    floors = runs[0].cursor["floors"]

    assert floors[str(gateway.id)] == (datetime.now(UTC).date() - timedelta(days=30)).isoformat()


async def test_an_unfinished_run_is_adopted_rather_than_replaced(
    platform: PlatformFixture,
) -> None:
    """A pass killed halfway left a ``running`` row with its floors in it. Starting fresh
    would mean re-walking every day it had already cleared."""
    async with platform.store.begin() as transaction:
        killed = await transaction.open_run("retention")
        await transaction.save_run(killed.id, cursor={"floors": {"x": "2026-01-01"}})
        await transaction.commit()
    platform.with_partitions_around()

    await platform.retention.run()

    runs = await _runs(platform, "retention")
    assert [run.id for run in runs] == [killed.id]
    assert runs[0].status == "succeeded"


async def test_the_platform_ceiling_makes_a_lenient_gateway_stricter_tonight(
    platform: PlatformFixture,
) -> None:
    """Capped, not skipped. An operator lowering a ceiling has to bind on gateways that
    were configured under the old one, without anybody re-saving them."""
    organization = uuid7()
    gateway = seed_gateway(platform, organization, name="chat", body_days=90, metadata_days=365)
    platform.with_partitions_around()
    old = seed_request(platform, gateway, days_ago=20)
    platform.retention.configured_with(
        PlatformSettings.model_validate({"retention": {"max_body_days": 7}})
    )

    await platform.retention.run()

    assert old not in platform.db.transcripts
    assert old in platform.db.request_logs


# ---------------------------------------------------------------------------
# expired facts
# ---------------------------------------------------------------------------


def seed_fact(
    platform: PlatformFixture, organization_id: uuid.UUID, *, expires_in_days: int | None
) -> MemoryFact:
    fact = MemoryFact(
        id=uuid7(),
        organization_id=organization_id,
        end_user_id=uuid7(),
        text="Travelling until the 14th.",
        kind="fact",
        confidence=1.0,
        expires_at=(
            None if expires_in_days is None else datetime.now(UTC) + timedelta(days=expires_in_days)
        ),
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    platform.db.memory_facts[fact.id] = fact
    return fact


async def test_an_expired_fact_leaves_both_stores(platform: PlatformFixture) -> None:
    """The criterion that makes this part of retention rather than a database chore: an
    orphaned vector is a fact that still shapes answers after it should have expired."""
    organization = uuid7()
    expired = seed_fact(platform, organization, expires_in_days=-1)
    live = seed_fact(platform, organization, expires_in_days=30)
    forever = seed_fact(platform, organization, expires_in_days=None)
    for fact in (expired, live, forever):
        await platform.index_fact(organization, fact.end_user_id, fact.id, fact.text)
    platform.with_partitions_around()

    report = await platform.retention.run()

    assert report.facts_expired == 1
    assert expired.id not in platform.db.memory_facts
    assert str(expired.id) not in await platform.facts.ids(organization)
    assert str(live.id) in await platform.facts.ids(organization)
    assert str(forever.id) in await platform.facts.ids(organization)


async def test_a_vector_that_could_not_be_removed_keeps_its_row(
    platform: PlatformFixture,
) -> None:
    """The ordering that matters. A row without its vector is recoverable on the next
    pass; deleting the row anyway would strand the vector permanently, and a stranded
    vector is a fact that keeps answering questions after it expired."""
    organization = uuid7()
    fact = seed_fact(platform, organization, expires_in_days=-1)
    await platform.index_fact(organization, fact.end_user_id, fact.id, fact.text)
    platform.with_partitions_around()

    async def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("qdrant is down")

    platform.facts.delete = refuse  # type: ignore[method-assign]

    report = await platform.retention.run()

    assert report.facts_expired == 0
    assert report.facts_stranded == 1
    assert fact.id in platform.db.memory_facts


async def _runs(platform: PlatformFixture, job: str) -> list[RunState]:
    async with platform.store.begin() as transaction:
        return [run for run in await transaction.recent_runs(limit=20) if run.job == job]
