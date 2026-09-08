"""The behaviour every metrics repository must have, written once.

The aggregation is the part of task 07 most likely to be quietly wrong, and "quietly" is
the operative word: a p95 computed two different ways still returns a plausible number.
So the fixture's latencies are chosen so that p50, p95 and p99 are three *different*
values — 5 to 100 in steps of five — and the expected answers below were worked out from
PostgreSQL's definition of ``percentile_disc`` by hand rather than from whatever the code
happened to return.

Two organizations throughout, because a single-tenant fixture passes every scoping check
for free.

Not a test module itself; it is the shared body the memory and PostgreSQL halves
parametrize over.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import pytest

from app.core.tenancy import TenantScope
from app.db.models import Organization
from app.services.metrics_store import LogFilters, MetricsRepository
from tests.monitoring_support import NOW, THROTTLED_AT

#: 5, 10, … 100. Twenty samples, so ``percentile_disc`` lands on index
#: ``ceil(fraction * 20) - 1`` — 9, 18 and 19 — which are 50, 95 and 100.
LATENCIES = tuple(range(5, 101, 5))
EXPECTED_P50 = 50
EXPECTED_P95 = 95
EXPECTED_P99 = 100


@dataclass
class Fixture:
    """Two organizations with traffic, and one gateway each."""

    repository: MetricsRepository
    acme: Organization
    globex: Organization
    acme_gateway_id: uuid.UUID
    other_gateway_id: uuid.UUID
    globex_gateway_id: uuid.UUID
    #: A request of Acme's that has a transcript stored.
    acme_log_id: uuid.UUID
    #: One of Acme's that does not, so "no transcript" is distinguishable from "no row".
    bodiless_log_id: uuid.UUID
    globex_log_id: uuid.UUID
    #: Task 14's throttled callers: three rejections and one, so the order is real.
    noisy_end_user_id: uuid.UUID
    quiet_end_user_id: uuid.UUID

    def throttling_window(self) -> LogFilters:
        """The window the rate-limit rejections live in — deliberately not the one every
        other check uses, so task 14's rows do not renumber task 07's expectations."""
        return self.window(
            start=THROTTLED_AT - timedelta(minutes=1), end=THROTTLED_AT + timedelta(minutes=1)
        )

    @property
    def acme_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.acme.id)

    @property
    def globex_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.globex.id)

    @property
    def platform_scope(self) -> TenantScope:
        return TenantScope(role="superadmin", organization_id=None)

    def window(self, **overrides: object) -> LogFilters:
        values: dict[str, object] = {
            "start": NOW - timedelta(hours=1),
            "end": NOW + timedelta(hours=1),
        }
        values.update(overrides)
        return LogFilters(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


async def a_summary_counts_only_this_organization(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        acme = await transaction.summary(fixture.window())
    async with fixture.repository.begin(fixture.globex_scope) as transaction:
        globex = await transaction.summary(fixture.window())

    assert acme.requests == len(LATENCIES) + 3
    assert globex.requests == 1
    assert acme.requests != globex.requests


async def a_platform_scope_sees_both(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.platform_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.requests == len(LATENCIES) + 4


async def another_organizations_request_is_not_found(fixture: Fixture) -> None:
    """404 rather than 403, like every other cross-tenant read in the system."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        assert await transaction.log(fixture.globex_log_id, fixture.window()) is None


async def listing_is_scoped(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.globex_scope) as transaction:
        rows = await transaction.logs(fixture.window(), after=None, limit=50)

    assert [row.id for row in rows] == [fixture.globex_log_id]


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------


async def rows_outside_the_window_are_excluded(fixture: Fixture) -> None:
    narrow = fixture.window(start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3))

    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(narrow)

    assert summary.requests == 0


async def an_empty_window_reports_no_percentiles(fixture: Fixture) -> None:
    """``None``, not zero. Zero would draw a latency chart claiming instant responses."""
    narrow = fixture.window(start=NOW + timedelta(hours=2), end=NOW + timedelta(hours=3))

    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(narrow)

    assert (summary.total.p50, summary.total.p95, summary.total.p99) == (None, None, None)


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------


async def percentiles_are_discrete_values_that_occurred(fixture: Fixture) -> None:
    """``percentile_disc``, not ``percentile_cont``: every answer is a latency some
    request actually took, so looking for the matching row always succeeds."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window(gateway_id=fixture.acme_gateway_id))

    assert summary.total.p50 == EXPECTED_P50
    assert summary.total.p95 == EXPECTED_P95
    assert summary.total.p99 == EXPECTED_P99


async def percentiles_ignore_rows_that_did_not_measure_it(fixture: Fixture) -> None:
    """Only three of Acme's requests streamed, so only three have a TTFT. Counting the
    rest as zero would report a first-token time no stream ever achieved."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.ttft.p50 == 20
    assert summary.ttft.p99 == 30


async def errors_are_counted_and_classified(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.errors == 2
    assert summary.status_classes["4xx"] == 1
    assert summary.status_classes["5xx"] == 1
    assert summary.status_classes["2xx"] == len(LATENCIES) + 1


async def the_error_taxonomy_groups_by_the_gateways_own_code(fixture: Fixture) -> None:
    """SPEC §10.1 asks for upstream errors, retrieval timeouts and rate-limit rejections
    as separate bars, and those are distinguishable only by the gateway's code — every
    one of them is a 5xx or a 429."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    codes = {group.error_code: group.requests for group in summary.error_groups}
    assert codes == {"upstream_timeout": 1, "model_not_found": 1}


async def tokens_are_summed(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.prompt_tokens == 300
    assert summary.completion_tokens == 150


async def traffic_is_attributed_per_model(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    busiest = summary.models[0]
    assert busiest.model_name == "acme-gpt"
    assert busiest.requests == len(LATENCIES)


async def the_error_rate_is_a_fraction_of_the_window(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.error_rate == 2 / (len(LATENCIES) + 3)


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


async def filtering_by_gateway_narrows_the_window(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window(gateway_id=fixture.other_gateway_id))

    assert summary.requests == 3


async def filtering_by_status_class_keeps_only_that_class(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(status_class="5xx"), after=None, limit=50)

    assert [row.status_code for row in rows] == [504]


async def filtering_by_minimum_latency_keeps_the_slow_ones(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(min_latency_ms=90), after=None, limit=50)

    assert [row.latency_total_ms for row in rows] == [100, 95, 90]


async def filtering_by_streamed_separates_the_two_paths(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(streamed=True), after=None, limit=50)

    assert len(rows) == 3
    assert all(row.streamed for row in rows)


async def free_text_searches_the_error_and_not_the_body(fixture: Fixture) -> None:
    """Deliberately narrow: searching prompts would mean scanning the large table this
    design exists to avoid, and would turn monitoring into content search over end-user
    data."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(
            fixture.window(search="did not respond"), after=None, limit=50
        )

    assert [row.error_code for row in rows] == ["upstream_timeout"]


async def free_text_matches_the_code_too(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(search="upstream_time"), after=None, limit=50)

    assert len(rows) == 1


async def filtering_by_session_finds_one_conversation(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(session_id="sess-7"), after=None, limit=50)

    assert len(rows) == 1


async def filtering_by_model_narrows_to_one_target(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())
        model_id = summary.models[0].upstream_model_id
        rows = await transaction.logs(
            fixture.window(upstream_model_id=model_id), after=None, limit=100
        )

    assert len(rows) == len(LATENCIES)


# ---------------------------------------------------------------------------
# paging
# ---------------------------------------------------------------------------


async def a_page_is_newest_first(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(), after=None, limit=5)

    ids = [row.id for row in rows]
    assert ids == sorted(ids, reverse=True)


async def a_page_over_fetches_by_one(fixture: Fixture) -> None:
    """How "is there another page" is answered without a second ``COUNT``."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.logs(fixture.window(), after=None, limit=5)

    assert len(rows) == 6


async def a_cursor_skips_what_came_before_it(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        # The store over-fetches by one, so the *page* is the first three and the fourth
        # row is only there to say another page exists. The cursor is the last row of the
        # page, which is what `page_of` hands the client.
        page = (await transaction.logs(fixture.window(), after=None, limit=3))[:3]
        second = await transaction.logs(fixture.window(), after=page[-1].id, limit=3)

    assert {row.id for row in page} & {row.id for row in second} == set()
    assert all(row.id < page[-1].id for row in second)


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def a_request_arrives_with_its_transcript(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        detail = await transaction.log(fixture.acme_log_id, fixture.window())

    assert detail is not None
    assert detail.transcript is not None
    assert detail.transcript.response_body == "the answer"


async def a_request_without_bodies_has_no_transcript(fixture: Fixture) -> None:
    """The row exists and the transcript does not, which is what lets the drawer say
    "not captured" rather than rendering an empty panel."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        detail = await transaction.log(fixture.bodiless_log_id, fixture.window())

    assert detail is not None
    assert detail.transcript is None


async def an_unknown_request_is_not_found(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        assert await transaction.log(uuid.uuid4(), fixture.window()) is None


# ---------------------------------------------------------------------------
# series
# ---------------------------------------------------------------------------


async def a_series_buckets_by_the_requested_interval(fixture: Fixture) -> None:
    """The three spread rows are 10 minutes apart, so 5-minute buckets separate them and
    an hour puts them together."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        fine = await transaction.timeseries(
            fixture.window(gateway_id=fixture.other_gateway_id),
            metric="requests",
            group_by="none",
            interval_seconds=300,
        )
        coarse = await transaction.timeseries(
            fixture.window(gateway_id=fixture.other_gateway_id),
            metric="requests",
            group_by="none",
            interval_seconds=3600,
        )

    assert len(fine) == 3
    assert len(coarse) == 1
    assert coarse[0].series["requests"] == 3


async def a_series_is_ordered_oldest_first(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.other_gateway_id),
            metric="requests",
            group_by="none",
            interval_seconds=300,
        )

    starts = [bucket.start for bucket in buckets]
    assert starts == sorted(starts)


async def a_series_can_be_grouped_by_status_class(fixture: Fixture) -> None:
    """The request-rate chart's status breakdown: one line per class, same buckets."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.other_gateway_id),
            metric="requests",
            group_by="status_class",
            interval_seconds=3600,
        )

    assert len(buckets) == 1
    assert buckets[0].series == {"2xx.requests": 1.0, "4xx.requests": 1.0, "5xx.requests": 1.0}


async def a_series_can_be_grouped_by_model(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.acme_gateway_id),
            metric="requests",
            group_by="model",
            interval_seconds=3600,
        )

    assert buckets[0].series == {"acme-gpt.requests": float(len(LATENCIES))}


async def a_series_can_be_grouped_by_gateway(fixture: Fixture) -> None:
    """What the gateways list's 24-hour column reads: one query for every row on the
    screen, keyed by id rather than by name — a name is not unique enough for a column."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(),
            metric="requests",
            group_by="gateway",
            interval_seconds=86_400,
        )

    totals: dict[str, float] = {}
    for bucket in buckets:
        for name, value in bucket.series.items():
            totals[name] = totals.get(name, 0) + value

    assert totals[f"{fixture.acme_gateway_id}.requests"] == float(len(LATENCIES))
    assert totals[f"{fixture.other_gateway_id}.requests"] == 3.0


async def a_token_series_carries_three_lines(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(), metric="tokens", group_by="none", interval_seconds=3600
        )

    totals = {
        name: sum(bucket.series.get(name, 0) for bucket in buckets)
        for name in ("prompt", "completion", "memory")
    }
    # Memory is non-zero because one seeded request injected documents. It is a separate
    # line rather than part of the prompt total precisely so an organization can see what
    # retrieval costs them (SPEC §10.1).
    assert totals == {"prompt": 300.0, "completion": 150.0, "memory": 180.0}


async def a_latency_series_carries_the_percentiles(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.acme_gateway_id),
            metric="latency",
            group_by="none",
            interval_seconds=3600,
        )

    assert buckets[0].series["total_p50"] == EXPECTED_P50
    assert buckets[0].series["total_p95"] == EXPECTED_P95


async def a_latency_series_omits_a_measurement_nothing_carried(fixture: Fixture) -> None:
    """No stream went to this gateway, so there is no TTFT line at all — as opposed to a
    line of zeroes, which would read as instant first tokens."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.acme_gateway_id),
            metric="latency",
            group_by="none",
            interval_seconds=3600,
        )

    assert "ttft_p95" not in buckets[0].series


async def the_empty_retrieval_rate_counts_only_searches_that_ran(fixture: Fixture) -> None:
    """Task 10 calls this the key quality signal, and the denominator is what makes it
    one: twenty of Acme's requests never searched, so counting them would dilute a
    two-in-three failure into one in eight and hide it."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.retrieval_attempts == 3
    assert summary.retrieval_empty == 2
    assert summary.empty_retrieval_rate == pytest.approx(2 / 3)


async def a_gateway_that_never_retrieved_has_no_rate_at_all(fixture: Fixture) -> None:
    """Zero over zero is not zero percent. A gateway with no connectors attached must
    read as "not applicable" rather than as a perfect score."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window(gateway_id=fixture.acme_gateway_id))

    assert summary.retrieval_attempts == 0
    assert summary.empty_retrieval_rate == 0.0


async def retrieval_percentiles_ignore_requests_that_did_not_search(
    fixture: Fixture,
) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.retrieval.p50 == 30
    assert summary.retrieval.p99 == 45


async def a_retrieval_series_carries_the_rate_and_the_counts(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.other_gateway_id),
            metric="retrieval",
            group_by="none",
            interval_seconds=86_400,
        )

    series = buckets[0].series
    assert series["attempts"] == 3.0
    assert series["empty"] == 2.0
    assert series["empty_rate"] == pytest.approx(2 / 3)
    assert series["p95"] == 45.0


async def a_retrieval_series_omits_the_rate_when_nothing_searched(
    fixture: Fixture,
) -> None:
    """A flat line at zero would read as "this gateway always finds what it needs"."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        buckets = await transaction.timeseries(
            fixture.window(gateway_id=fixture.acme_gateway_id),
            metric="retrieval",
            group_by="none",
            interval_seconds=86_400,
        )

    assert buckets[0].series["attempts"] == 0.0
    assert "empty_rate" not in buckets[0].series


async def memory_tokens_are_summed_separately_from_the_rest(fixture: Fixture) -> None:
    """SPEC §10.1: an organization has to be able to see what retrieval costs them,
    which means it cannot be folded into the prompt total."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        summary = await transaction.summary(fixture.window())

    assert summary.memory_tokens == 180


Check = Callable[[Fixture], Awaitable[None]]

#: Every check, in one list, so neither implementation can be given a shorter exam.
# ---------------------------------------------------------------------------
# throttling (SPEC §11)
# ---------------------------------------------------------------------------


async def throttled_callers_are_ranked_by_how_often_they_were_refused(
    fixture: Fixture,
) -> None:
    """Worst first, because the answer being looked for is "which integration"."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.throttled_end_users(fixture.throttling_window())

    assert [(row.end_user_id, row.rejections) for row in rows] == [
        (fixture.noisy_end_user_id, 3),
        (fixture.quiet_end_user_id, 1),
    ]


async def throttling_is_scoped_to_one_organization(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.globex_scope) as transaction:
        rows = await transaction.throttled_end_users(fixture.throttling_window())

    assert len(rows) == 1
    assert rows[0].end_user_id not in {fixture.noisy_end_user_id, fixture.quiet_end_user_id}


async def an_unidentified_caller_is_not_a_row_on_the_list(fixture: Fixture) -> None:
    """A gateway that identifies nobody would otherwise produce one enormous bar that is
    not a caller and cannot be acted on."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.throttled_end_users(fixture.throttling_window())

    assert all(row.end_user_id is not None for row in rows)
    assert sum(row.rejections for row in rows) == 4


async def only_rate_limit_rejections_count_as_throttling(fixture: Fixture) -> None:
    """The window every other check uses is full of failures, and none of them is one."""
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.throttled_end_users(fixture.window())

    assert rows == []


async def throttling_can_be_narrowed_to_one_gateway(fixture: Fixture) -> None:
    async with fixture.repository.begin(fixture.acme_scope) as transaction:
        rows = await transaction.throttled_end_users(
            fixture.window(
                start=THROTTLED_AT - timedelta(minutes=1),
                end=THROTTLED_AT + timedelta(minutes=1),
                gateway_id=fixture.other_gateway_id,
            )
        )

    assert [(row.end_user_id, row.rejections) for row in rows] == [(fixture.noisy_end_user_id, 1)]


CHECKS: tuple[Check, ...] = (
    a_summary_counts_only_this_organization,
    a_platform_scope_sees_both,
    another_organizations_request_is_not_found,
    listing_is_scoped,
    rows_outside_the_window_are_excluded,
    an_empty_window_reports_no_percentiles,
    percentiles_are_discrete_values_that_occurred,
    percentiles_ignore_rows_that_did_not_measure_it,
    errors_are_counted_and_classified,
    the_error_taxonomy_groups_by_the_gateways_own_code,
    tokens_are_summed,
    traffic_is_attributed_per_model,
    the_error_rate_is_a_fraction_of_the_window,
    filtering_by_gateway_narrows_the_window,
    filtering_by_status_class_keeps_only_that_class,
    filtering_by_minimum_latency_keeps_the_slow_ones,
    filtering_by_streamed_separates_the_two_paths,
    free_text_searches_the_error_and_not_the_body,
    free_text_matches_the_code_too,
    filtering_by_session_finds_one_conversation,
    filtering_by_model_narrows_to_one_target,
    a_page_is_newest_first,
    a_page_over_fetches_by_one,
    a_cursor_skips_what_came_before_it,
    a_request_arrives_with_its_transcript,
    a_request_without_bodies_has_no_transcript,
    an_unknown_request_is_not_found,
    a_series_buckets_by_the_requested_interval,
    a_series_is_ordered_oldest_first,
    a_series_can_be_grouped_by_status_class,
    a_series_can_be_grouped_by_model,
    a_series_can_be_grouped_by_gateway,
    a_token_series_carries_three_lines,
    a_latency_series_carries_the_percentiles,
    a_latency_series_omits_a_measurement_nothing_carried,
    the_empty_retrieval_rate_counts_only_searches_that_ran,
    a_gateway_that_never_retrieved_has_no_rate_at_all,
    retrieval_percentiles_ignore_requests_that_did_not_search,
    a_retrieval_series_carries_the_rate_and_the_counts,
    a_retrieval_series_omits_the_rate_when_nothing_searched,
    memory_tokens_are_summed_separately_from_the_rest,
    throttled_callers_are_ranked_by_how_often_they_were_refused,
    throttling_is_scoped_to_one_organization,
    an_unidentified_caller_is_not_a_row_on_the_list,
    only_rate_limit_rejections_count_as_throttling,
    throttling_can_be_narrowed_to_one_gateway,
)
