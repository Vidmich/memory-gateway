"""Bucket selection, filter validation, the summary cache, and the id-derived window.

The store answers the questions; this module is about which questions the API will let
somebody ask, and what it does when Redis is not there.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.services.memory_db import MemoryDatabase
from app.services.monitoring import (
    INTERVALS,
    MAX_BUCKETS,
    MAX_WINDOW,
    build_filters,
    check_metric,
    choose_interval,
)
from tests.auth_support import make_organization
from tests.monitoring_support import (
    NOW,
    BrokenSummaryCache,
    FakeSummaryCache,
    build_monitoring,
    make_log_row,
)


def actor_for(organization_id: uuid.UUID) -> Actor:
    return Actor(
        user_id=uuid7(), scope=TenantScope(role="org_admin", organization_id=organization_id)
    )


def window(hours: float = 1) -> tuple[datetime, datetime]:
    return NOW - timedelta(hours=hours), NOW


# ---------------------------------------------------------------------------
# bucket selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (1, 60),  # the work item's first example
        (2, 60),
        (6, 300),
        (24, 300),
        (24 * 7, 1800),
        (24 * 30, 3600),  # the work item's second example
    ],
)
def test_the_range_picks_the_bucket(hours: float, expected: int) -> None:
    """Server-side, so the client never fetches raw rows to aggregate them itself."""
    start, end = window(hours)

    assert choose_interval(start, end) == expected


def test_a_requested_interval_is_snapped_to_the_ladder() -> None:
    """Not refused, and not honoured literally. An arbitrary interval would make two
    charts of the same data disagree about where a bucket starts."""
    start, end = window(1)

    assert choose_interval(start, end, requested=90) == 300


def test_a_requested_interval_is_widened_until_the_answer_fits() -> None:
    """The bound is on the size of the response, not on the caller's good manners:
    ``?from=<a year ago>&interval=60`` must not produce half a million buckets."""
    end = NOW
    start = end - timedelta(days=60)

    interval = choose_interval(start, end, requested=60)

    assert (end - start).total_seconds() / interval <= MAX_BUCKETS


def test_no_range_ever_exceeds_the_bucket_cap() -> None:
    for days in (1, 7, 30, MAX_WINDOW.days):
        start, end = NOW - timedelta(days=days), NOW
        interval = choose_interval(start, end)
        assert (end - start).total_seconds() / interval <= MAX_BUCKETS, days


def test_every_chosen_interval_is_on_the_ladder() -> None:
    for hours in (0.5, 1, 3, 12, 24, 24 * 3, 24 * 10, 24 * 45, 24 * 90):
        start, end = window(hours)
        assert choose_interval(start, end) in INTERVALS, hours


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


def test_the_default_window_is_the_last_day() -> None:
    """What the screen opens on, so an unparameterised request is the useful one."""
    filters = build_filters(start=None, end=None)

    assert timedelta(hours=23, minutes=59) < filters.end - filters.start <= timedelta(hours=24)


def test_a_backwards_window_is_refused() -> None:
    with pytest.raises(Validation) as caught:
        build_filters(start=NOW, end=NOW - timedelta(hours=1))

    assert caught.value.param == "to"


def test_an_over_wide_window_is_refused_rather_than_clamped() -> None:
    """Silently returning ninety days when the caller asked for a year produces a chart
    with the wrong denominator, and nothing on it says so."""
    with pytest.raises(Validation) as caught:
        build_filters(start=NOW - timedelta(days=400), end=NOW)

    assert caught.value.param == "from"
    assert str(MAX_WINDOW.days) in caught.value.message


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Hand-typed URLs carry no zone, and there is exactly one sensible reading."""
    filters = build_filters(start=datetime(2026, 9, 7, 11), end=datetime(2026, 9, 7, 12))

    assert filters.start.tzinfo is not None
    assert filters.start == datetime(2026, 9, 7, 11, tzinfo=UTC)


def test_an_unknown_status_class_is_refused() -> None:
    with pytest.raises(Validation) as caught:
        build_filters(start=None, end=None, status_class="6xx")

    assert caught.value.param == "status_class"


def test_search_is_trimmed_and_empty_becomes_nothing() -> None:
    assert build_filters(start=None, end=None, search="   ").search is None
    assert build_filters(start=None, end=None, search="  boom ").search == "boom"


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_a_known_metric_and_grouping_are_accepted() -> None:
    assert check_metric("requests", "status_class") == ("requests", "status_class")


@pytest.mark.parametrize(("metric", "group_by"), [("nope", "none"), ("requests", "nope")])
def test_an_unknown_enum_is_refused(metric: str, group_by: str) -> None:
    with pytest.raises(Validation):
        check_metric(metric, group_by)


def test_only_a_count_can_be_grouped() -> None:
    """A p95 over the 5xx requests is a number about failures, not about latency, and
    drawing it beside the overall p95 invites exactly the wrong conclusion."""
    with pytest.raises(Validation) as caught:
        check_metric("latency", "status_class")

    assert caught.value.param == "group_by"


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


def seeded() -> tuple[MemoryDatabase, Actor]:
    database = MemoryDatabase()
    organization = make_organization()
    database.add_organization(organization)
    row = make_log_row(organization, status_code=200, latency_total_ms=17)
    database.request_logs[row.id] = row
    return database, actor_for(organization.id)


async def test_a_second_identical_summary_comes_from_the_cache() -> None:
    database, actor = seeded()
    cache = FakeSummaryCache()
    service = build_monitoring(database, cache=cache)
    filters = build_filters(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))

    first = await service.summary(actor, filters)
    second = await service.summary(actor, filters)

    assert cache.hits == 1
    assert (first.requests, first.total.p50) == (second.requests, second.total.p50)


async def test_a_different_window_is_a_different_answer() -> None:
    database, actor = seeded()
    cache = FakeSummaryCache()
    service = build_monitoring(database, cache=cache)

    await service.summary(actor, build_filters(start=NOW - timedelta(hours=1), end=NOW))
    await service.summary(actor, build_filters(start=NOW - timedelta(hours=2), end=NOW))

    assert cache.hits == 0
    assert cache.puts == 2


async def test_two_organizations_never_share_a_cache_entry() -> None:
    """The organization is *in* the key. Keyed on the filters alone, one tenant's totals
    would be served to another the moment both picked the same range."""
    database, actor = seeded()
    other = make_organization(name="Globex", slug="globex")
    database.add_organization(other)
    row = make_log_row(other)
    database.request_logs[row.id] = row

    cache = FakeSummaryCache()
    service = build_monitoring(database, cache=cache)
    filters = build_filters(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))

    await service.summary(actor, filters)
    theirs = await service.summary(actor_for(other.id), filters)

    assert cache.hits == 0
    assert theirs.requests == 1


async def test_a_broken_cache_costs_latency_not_correctness() -> None:
    """A monitoring screen going blank when Redis restarts is the worst possible moment
    for a monitoring screen to go blank."""
    database, actor = seeded()
    service = build_monitoring(database, cache=BrokenSummaryCache())

    summary = await service.summary(
        actor, build_filters(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))
    )

    assert summary.requests == 1


async def test_a_cached_summary_round_trips_through_json() -> None:
    """The cache stores plain JSON, so the rebuild has to put the UUIDs back."""
    database, actor = seeded()
    row = next(iter(database.request_logs.values()))
    row.upstream_model_id = uuid7()
    row.model_name = "acme-gpt"

    cache = FakeSummaryCache()
    service = build_monitoring(database, cache=cache)
    filters = build_filters(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))

    await service.summary(actor, filters)
    # Force the round trip the way Redis would: values in, text out, values back.
    import json

    for key, value in list(cache.values.items()):
        cache.values[key] = json.loads(json.dumps(value, default=str))
    rebuilt = await service.summary(actor, filters)

    assert rebuilt.models[0].upstream_model_id == row.upstream_model_id
    assert rebuilt.models[0].model_name == "acme-gpt"


# ---------------------------------------------------------------------------
# the detail window
# ---------------------------------------------------------------------------


async def test_a_request_is_found_by_id_alone() -> None:
    """No time range from the caller: the id is a UUIDv7 and says which day to look in.

    That is task 01's choice of UUIDv7 over UUIDv4 paying off six tasks later — the
    alternative is probing every partition retained.
    """
    database = MemoryDatabase()
    organization = make_organization()
    database.add_organization(organization)
    row = make_log_row(organization, created_at=datetime.now(UTC))
    database.request_logs[row.id] = row
    service = build_monitoring(database)

    detail = await service.get_log(actor_for(organization.id), row.id)

    assert detail.log.id == row.id


async def test_a_request_whose_row_is_gone_is_not_found() -> None:
    database = MemoryDatabase()
    organization = make_organization()
    database.add_organization(organization)
    service = build_monitoring(database)

    with pytest.raises(NotFound):
        await service.get_log(actor_for(organization.id), uuid7())


async def test_an_id_this_system_never_minted_is_not_found() -> None:
    """A UUIDv4 carries no timestamp, so there is no window to look in — and no row of
    ours could have that id anyway. A 404, not a 500."""
    database = MemoryDatabase()
    organization = make_organization()
    database.add_organization(organization)
    service = build_monitoring(database)

    with pytest.raises(NotFound):
        await service.get_log(actor_for(organization.id), uuid.uuid4())


async def test_paging_the_log_list_hands_back_a_cursor() -> None:
    database = MemoryDatabase()
    organization = make_organization()
    database.add_organization(organization)
    for _ in range(5):
        row = make_log_row(organization)
        database.request_logs[row.id] = row
    service = build_monitoring(database)
    filters = build_filters(start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1))

    page = await service.list_logs(actor_for(organization.id), filters, limit=2)

    assert len(page.items) == 2
    assert page.next_cursor is not None


async def test_the_series_reports_the_interval_it_used() -> None:
    """A chart that labels its axis from the request rather than the response is a chart
    that lies about its own resolution."""
    database, actor = seeded()
    service = build_monitoring(database)

    series = await service.timeseries(
        actor,
        build_filters(start=NOW - timedelta(days=30), end=NOW),
        interval_seconds=60,
    )

    assert series.interval_seconds > 60
