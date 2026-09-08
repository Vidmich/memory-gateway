"""``GET /api/v1/audit-events`` and its CSV export.

The hooks that fill this table are covered in ``tests/test_audit_hooks.py``; here the
subject is the screen: what a page looks like on the wire, what the filters mean, who may
read it, and what the export produces.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from httpx import Response

from app.core.ids import uuid7
from app.services.audit_service import EXPORT_COLUMNS, MAX_EXPORT_ROWS, TRUNCATION_NOTICE
from tests.audit_store_contract import NOW, make_event
from tests.conftest import AuthHarness

EVENTS = "/api/v1/audit-events"
EXPORT = f"{EVENTS}/export"


def seed(harness: AuthHarness, **overrides: Any) -> Any:
    """Put one event straight into the shared rows, the way a mutation would."""
    organization = harness.auth.organization
    assert organization is not None
    overrides.setdefault("organization_id", organization.id)
    return harness.auth.database.add_audit_event(make_event(**overrides))


async def read(harness: AuthHarness, token: str, query: str = "") -> Response:
    return await harness.client.get(f"{EVENTS}{query}", headers=harness.bearer(token))


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


# ---------------------------------------------------------------------------
# the list
# ---------------------------------------------------------------------------


async def test_an_empty_log_is_a_page_not_an_error(auth_harness: AuthHarness, token: str) -> None:
    response = await read(auth_harness, token)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_an_event_carries_who_what_and_where_from(
    auth_harness: AuthHarness, token: str
) -> None:
    event = seed(
        auth_harness,
        action="gateway.update",
        changes=[{"path": "system_context", "before": "old", "after": "new"}],
    )

    body = (await read(auth_harness, token)).json()["items"][0]

    assert body["id"] == str(event.id)
    assert body["action"] == "gateway.update"
    assert body["actor"] == "ada@example.com"
    assert body["actor_type"] == "user"
    assert body["target_type"] == "gateway"
    assert body["target"] == "support"
    assert body["ip"] == "203.0.113.7"
    assert body["request_id"] == "req-1"


async def test_a_change_is_typed_by_what_happened_to_the_field(
    auth_harness: AuthHarness, token: str
) -> None:
    """``added``/``removed``/``changed``, so the UI switches on one field rather than
    inspecting which keys the JSON happens to carry."""
    seed(
        auth_harness,
        changes=[
            {"path": "name", "before": "old", "after": "new"},
            {"path": "extra_headers.x-region", "after": "eu"},
            {"path": "system_context", "before": "gone"},
        ],
    )

    event = (await read(auth_harness, token)).json()["items"][0]
    changes = {row["path"]: row for row in event["changes"]}

    assert changes["name"]["kind"] == "changed"
    assert changes["extra_headers.x-region"]["kind"] == "added"
    assert changes["system_context"]["kind"] == "removed"


async def test_a_bulk_summary_survives_to_the_wire(auth_harness: AuthHarness, token: str) -> None:
    organization = auth_harness.auth.organization
    assert organization is not None
    event = make_event(organization_id=organization.id, action="connector.resync")
    event.diff = {"changes": [], "summary": {"count": 12, "sample": ["a.md"], "added": 3}}
    auth_harness.auth.database.add_audit_event(event)

    body = (await read(auth_harness, token)).json()["items"][0]

    assert body["summary"] == {"count": 12, "sample": ["a.md"], "added": 3}


async def test_events_are_newest_first(auth_harness: AuthHarness, token: str) -> None:
    first = seed(auth_harness, action="gateway.create")
    second = seed(auth_harness, action="gateway.update")

    items = (await read(auth_harness, token)).json()["items"]

    assert [item["id"] for item in items] == [str(second.id), str(first.id)]


async def test_a_page_hands_back_a_cursor(auth_harness: AuthHarness, token: str) -> None:
    for _ in range(3):
        seed(auth_harness)

    page = (await read(auth_harness, token, "?limit=2")).json()
    assert len(page["items"]) == 2
    assert page["next_cursor"] is not None

    rest = (await read(auth_harness, token, f"?limit=2&cursor={page['next_cursor']}")).json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


async def test_filtering_by_action(auth_harness: AuthHarness, token: str) -> None:
    seed(auth_harness, action="gateway.update")
    wanted = seed(auth_harness, action="key.revoke")

    items = (await read(auth_harness, token, "?action=key.revoke")).json()["items"]

    assert [item["id"] for item in items] == [str(wanted.id)]


async def test_filtering_by_target_is_one_objects_history(
    auth_harness: AuthHarness, token: str
) -> None:
    """The contextual "Audit" tab on a gateway, model or connector screen — which is
    where this log is actually read."""
    gateway_id = uuid7()
    seed(auth_harness, target_type="gateway", target_id=gateway_id, action="gateway.create")
    seed(auth_harness, target_type="gateway", target_id=gateway_id, action="gateway.update")
    seed(auth_harness, target_type="gateway", target_id=uuid7(), action="gateway.update")

    items = (
        await read(auth_harness, token, f"?target_type=gateway&target_id={gateway_id}")
    ).json()["items"]

    assert {item["action"] for item in items} == {"gateway.create", "gateway.update"}
    assert len(items) == 2


async def test_filtering_by_actor(auth_harness: AuthHarness, token: str) -> None:
    ada = uuid7()
    seed(auth_harness, actor_user_id=ada)
    seed(auth_harness, actor_user_id=uuid7())

    items = (await read(auth_harness, token, f"?actor_user_id={ada}")).json()["items"]

    assert len(items) == 1


async def test_filtering_by_a_time_range(auth_harness: AuthHarness, token: str) -> None:
    from datetime import timedelta

    old = seed(auth_harness, created_at=NOW - timedelta(days=30))
    recent = seed(auth_harness, created_at=NOW)

    since = (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    items = (await read(auth_harness, token, f"?from={since}")).json()["items"]

    assert [item["id"] for item in items] == [str(recent.id)]
    assert str(old.id) not in {item["id"] for item in items}


async def test_an_unknown_target_type_is_refused_rather_than_returning_nothing(
    auth_harness: AuthHarness, token: str
) -> None:
    """A typo that returns an empty list reads as "nothing ever happened", which is the
    one answer this screen must not give by accident."""
    response = await read(auth_harness, token, "?target_type=gatway")

    assert response.status_code == 422
    assert "gateway" in response.text


async def test_a_backwards_window_is_refused(auth_harness: AuthHarness, token: str) -> None:
    response = await read(auth_harness, token, "?from=2026-03-04T12:00:00Z&to=2026-03-01T00:00:00Z")

    assert response.status_code == 422


async def test_there_is_no_default_window(auth_harness: AuthHarness, token: str) -> None:
    """Unlike the request log. "When did this change" is usually answered by something
    older than a day, and a silent 24-hour default would hide it."""
    from datetime import timedelta

    seed(auth_harness, created_at=NOW - timedelta(days=400))

    assert len((await read(auth_harness, token)).json()["items"]) == 1


# ---------------------------------------------------------------------------
# immutability, from the outside
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
async def test_no_api_path_changes_an_event(
    auth_harness: AuthHarness, token: str, method: str
) -> None:
    """The acceptance criterion: events are immutable, and there is no route at all."""
    event = seed(auth_harness)

    response = await auth_harness.client.request(
        method, f"{EVENTS}/{event.id}", headers=auth_harness.bearer(token), json={}
    )

    assert response.status_code in (404, 405)


# ---------------------------------------------------------------------------
# the export
# ---------------------------------------------------------------------------


def rows(response: Response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


async def test_the_export_is_a_csv_file_with_a_header(
    auth_harness: AuthHarness, token: str
) -> None:
    seed(auth_harness, action="gateway.update")

    response = await auth_harness.client.get(EXPORT, headers=auth_harness.bearer(token))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert rows(response)[0] == list(EXPORT_COLUMNS)


async def test_an_exported_row_carries_the_event(auth_harness: AuthHarness, token: str) -> None:
    seed(
        auth_harness,
        action="model.update",
        target_type="upstream_model",
        target_label="acme-gpt",
        changes=[{"path": "credential", "before": "***", "after": "***"}],
    )

    row = dict(zip(EXPORT_COLUMNS, rows(await _export(auth_harness, token))[1], strict=True))

    assert row["action"] == "model.update"
    assert row["target"] == "acme-gpt"
    assert row["actor"] == "ada@example.com"
    assert row["changes"] == "credential: *** -> ***"


async def test_the_export_honours_the_filter(auth_harness: AuthHarness, token: str) -> None:
    seed(auth_harness, action="gateway.update")
    seed(auth_harness, action="key.revoke")

    response = await _export(auth_harness, token, "?action=key.revoke")

    assert len(rows(response)) == 2  # the header and one event


async def test_the_export_pages_through_everything(auth_harness: AuthHarness, token: str) -> None:
    """More rows than one page: the stream has to continue past its own cursor."""
    from app.services.audit_service import EXPORT_PAGE_SIZE

    for _ in range(EXPORT_PAGE_SIZE + 5):
        seed(auth_harness)

    assert len(rows(await _export(auth_harness, token))) == EXPORT_PAGE_SIZE + 6


async def test_the_export_is_rate_limited(auth_harness: AuthHarness, token: str) -> None:
    """An export is the cheapest way to pull a whole history; repeating it in a loop is
    the access pattern worth a ceiling."""
    from app.services.audit_service import AuditService
    from app.services.login_throttle import MemoryThrottleStore
    from app.services.rate_limit import FixedWindowLimiter

    # Built once, outside the lambda: a limiter constructed per request counts to one
    # forever, which is a test that would pass without the feature existing.
    limited = AuditService(
        auth_harness.auth.audit_store,
        export_limiter=FixedWindowLimiter(
            store=MemoryThrottleStore(), action="audit-export", limit=1, window_seconds=60
        ),
    )
    auth_harness.app.dependency_overrides[_dependency(auth_harness)] = lambda: limited

    assert (await _export(auth_harness, token)).status_code == 200
    refused = await auth_harness.client.get(EXPORT, headers=auth_harness.bearer(token))

    assert refused.status_code == 429
    assert "retry-after" in refused.headers


async def test_the_ceiling_is_reported_inside_the_file(
    auth_harness: AuthHarness, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response whose body has started cannot change its status code, so the truncation
    is a row rather than an error — with an empty id, which is what says it is not one."""
    import app.services.audit_service as service_module

    monkeypatch.setattr(service_module, "MAX_EXPORT_ROWS", 2)
    monkeypatch.setattr(service_module, "EXPORT_PAGE_SIZE", 2)
    for _ in range(4):
        seed(auth_harness)

    exported = rows(await _export(auth_harness, token))

    assert len(exported) == 4  # header, two events, the notice
    assert exported[-1][0] == ""
    assert exported[-1][EXPORT_COLUMNS.index("action")].startswith("(truncated")


def test_the_truncation_notice_names_the_cap() -> None:
    assert f"{MAX_EXPORT_ROWS:,}" in TRUNCATION_NOTICE


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------


async def test_reading_the_log_needs_a_session(auth_harness: AuthHarness) -> None:
    assert (await auth_harness.client.get(EVENTS)).status_code == 401


async def _export(harness: AuthHarness, token: str, query: str = "") -> Response:
    return await harness.client.get(f"{EXPORT}{query}", headers=harness.bearer(token))


def _dependency(harness: AuthHarness) -> Any:
    from app.api.control.deps import get_audit_service

    return get_audit_service
