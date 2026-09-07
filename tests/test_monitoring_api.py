"""``/api/v1/metrics`` and ``/api/v1/logs`` over HTTP.

The routes are thin, so most of what is asserted here is the wiring: that a query string
becomes the filter it looks like, that every role in the organization can read, and that
the detail response tells the drawer *why* a body is missing rather than leaving it to
guess.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest

from app.db.models import Transcript
from tests.conftest import DirectoryHarness
from tests.monitoring_support import NOW, make_log_row

WINDOW = {
    "from": (NOW - timedelta(hours=1)).isoformat(),
    "to": (NOW + timedelta(hours=1)).isoformat(),
}


def query(**extra: Any) -> str:
    """Properly encoded, because an ISO timestamp ends in ``+00:00`` and a raw ``+`` in a
    query string is a space — which turns every one of these into a 422 about the date."""
    return urlencode({**WINDOW, **extra})


def seed(directory: DirectoryHarness, **overrides: Any) -> Any:
    world = directory.world
    overrides.setdefault("gateway_id", world.acme_gateway.id)
    row = make_log_row(world.acme, **overrides)
    world.database.request_logs[row.id] = row
    return row


# ---------------------------------------------------------------------------
# who may read
# ---------------------------------------------------------------------------

#: SPEC §5.2's read roles. Bodies contain end-user content and a viewer can read them —
#: see the note in ``app/api/control/monitoring.py`` for why that is the right trade.
READERS = ("acme_admin", "acme_member", "acme_viewer")


@pytest.mark.parametrize("person", READERS)
@pytest.mark.parametrize(
    "path", ["/api/v1/metrics/summary", "/api/v1/metrics/timeseries", "/api/v1/logs"]
)
async def test_every_role_in_the_organization_may_read(
    person: str, path: str, directory: DirectoryHarness
) -> None:
    response = await directory.as_user(directory.world.people[person], "GET", f"{path}?{query()}")

    assert response.status_code == 200


async def test_reading_requires_a_session(directory: DirectoryHarness) -> None:
    response = await directory.client.get("/api/v1/logs")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# ---------------------------------------------------------------------------
# the summary
# ---------------------------------------------------------------------------


async def test_the_summary_counts_this_organizations_traffic(
    directory: DirectoryHarness,
) -> None:
    seed(directory, status_code=200, latency_total_ms=10)
    seed(directory, status_code=500, latency_total_ms=30, error_code="upstream_error")

    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/metrics/summary?{query()}"
    )
    body = response.json()

    # One row is seeded by the fixture world itself, so the window holds three.
    assert body["requests"] == 3
    assert body["errors"] == 1
    assert body["error_rate"] == pytest.approx(1 / 3)
    assert body["status_classes"] == {"2xx": 2, "5xx": 1}
    assert body["error_groups"] == [{"error_code": "upstream_error", "requests": 1}]


async def test_the_summary_is_filtered_by_gateway(directory: DirectoryHarness) -> None:
    from app.core.ids import uuid7

    seed(directory)
    other = uuid7()
    seed(directory, gateway_id=other)

    response = await directory.as_user(
        directory.world.acme_admin,
        "GET",
        f"/api/v1/metrics/summary?{query(gateway_id=other)}",
    )

    assert response.json()["requests"] == 1


async def test_percentiles_are_null_when_nothing_measured_them(
    directory: DirectoryHarness,
) -> None:
    """Not zero. A TTFT chart of zeroes claims instant first tokens."""
    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/metrics/summary?{query()}"
    )

    assert response.json()["ttft"] == {"p50": None, "p95": None, "p99": None}


async def test_a_backwards_window_is_a_422(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin,
        "GET",
        "/api/v1/metrics/summary?"
        + urlencode({"from": NOW.isoformat(), "to": (NOW - timedelta(days=1)).isoformat()}),
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "to"


# ---------------------------------------------------------------------------
# the series
# ---------------------------------------------------------------------------


async def test_a_series_reports_the_interval_it_used(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/metrics/timeseries?{query()}"
    )
    body = response.json()

    assert body["interval_seconds"] == 60
    assert isinstance(body["buckets"], list)


async def test_a_series_can_be_grouped_by_status_class(directory: DirectoryHarness) -> None:
    seed(directory, status_code=500)

    response = await directory.as_user(
        directory.world.acme_admin,
        "GET",
        f"/api/v1/metrics/timeseries?{query(metric='requests', group_by='status_class')}",
    )
    names = {name for bucket in response.json()["buckets"] for name in bucket["series"]}

    assert names == {"2xx.requests", "5xx.requests"}


async def test_a_series_can_be_grouped_by_gateway(directory: DirectoryHarness) -> None:
    """What the gateways list reads for its 24-hour column: one query, keyed by id."""
    seed(directory)

    response = await directory.as_user(
        directory.world.acme_admin,
        "GET",
        f"/api/v1/metrics/timeseries?{query(metric='requests', group_by='gateway')}",
    )
    names = {name for bucket in response.json()["buckets"] for name in bucket["series"]}

    assert f"{directory.world.acme_gateway.id}.requests" in names


async def test_an_unknown_metric_is_a_422(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/metrics/timeseries?{query(metric='vibes')}"
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "metric"


async def test_grouping_a_latency_series_is_refused(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin,
        "GET",
        f"/api/v1/metrics/timeseries?{query(metric='latency', group_by='model')}",
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "group_by"


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------


async def test_the_list_is_newest_first_and_pages(directory: DirectoryHarness) -> None:
    for _ in range(4):
        seed(directory)

    first = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/logs?{query(limit=2)}"
    )
    body = first.json()
    second = await directory.as_user(
        directory.world.acme_admin,
        "GET",
        f"/api/v1/logs?{query(limit=2, cursor=body['next_cursor'])}",
    )

    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    assert {item["id"] for item in body["items"]} & {
        item["id"] for item in second.json()["items"]
    } == set()


async def test_the_list_carries_no_bodies(directory: DirectoryHarness) -> None:
    """The split exists so the table never touches the large text columns; the response
    shape is what makes that visible to anyone reading it."""
    row = seed(directory)
    directory.world.database.transcripts[row.id] = Transcript(
        request_log_id=row.id,
        created_at=row.created_at,
        organization_id=row.organization_id,
        response_body="never in a list",
    )

    response = await directory.as_user(directory.world.acme_admin, "GET", f"/api/v1/logs?{query()}")

    assert "never in a list" not in response.text


async def test_the_list_filters_on_status_class(directory: DirectoryHarness) -> None:
    seed(directory, status_code=404, error_code="model_not_found")

    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/logs?{query(status_class='4xx')}"
    )
    items = response.json()["items"]

    assert [item["status_code"] for item in items] == [404]


async def test_the_list_searches_the_error_text(directory: DirectoryHarness) -> None:
    seed(directory, status_code=504, error_code="upstream_timeout", error_message="did not respond")

    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/logs?{query(search='did not')}"
    )

    assert len(response.json()["items"]) == 1


# ---------------------------------------------------------------------------
# the detail
# ---------------------------------------------------------------------------


async def test_a_request_arrives_with_its_transcript(directory: DirectoryHarness) -> None:
    row = seed(directory)
    directory.world.database.transcripts[row.id] = Transcript(
        request_log_id=row.id,
        created_at=row.created_at,
        organization_id=row.organization_id,
        request_body=[{"role": "user", "content": "hello"}],
        assembled_prompt=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hello"},
        ],
        response_body="hi",
    )

    response = await directory.as_user(directory.world.acme_admin, "GET", f"/api/v1/logs/{row.id}")
    body = response.json()

    assert body["log"]["id"] == str(row.id)
    assert body["transcript"]["response_body"] == "hi"
    assert body["transcript"]["assembled_prompt"][0]["role"] == "system"


async def test_a_request_with_nothing_stored_says_so(directory: DirectoryHarness) -> None:
    """``transcript: null`` plus ``bodies_omitted`` is what lets the drawer distinguish
    "logging was off" from "the queue was full" — an empty panel says neither."""
    row = seed(directory, bodies_omitted="queue_pressure")

    response = await directory.as_user(directory.world.acme_admin, "GET", f"/api/v1/logs/{row.id}")
    body = response.json()

    assert body["transcript"] is None
    assert body["log"]["bodies_omitted"] == "queue_pressure"


async def test_the_detail_needs_no_time_range(directory: DirectoryHarness) -> None:
    """The id is a UUIDv7 and carries the day; the server works out the partition."""
    row = seed(directory)

    response = await directory.as_user(directory.world.acme_admin, "GET", f"/api/v1/logs/{row.id}")

    assert response.status_code == 200


async def test_an_unknown_request_is_a_404(directory: DirectoryHarness) -> None:
    from app.core.ids import uuid7

    response = await directory.as_user(directory.world.acme_admin, "GET", f"/api/v1/logs/{uuid7()}")

    assert response.status_code == 404


async def test_a_superadmin_reads_an_organization_by_assuming_it(
    directory: DirectoryHarness,
) -> None:
    world = directory.world

    response = await directory.as_user(
        world.superadmin, "GET", f"/api/v1/logs/{world.acme_log.id}", assuming=world.acme.id
    )

    assert response.status_code == 200
    assert response.json()["log"]["id"] == str(world.acme_log.id)
