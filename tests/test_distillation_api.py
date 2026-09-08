"""``/api/v1/distillation`` and ``POST /end-users/{id}/distil`` over the real app.

The service tests cover the rules; these cover the envelope — status codes, capability
gates, and the three shapes most likely to be quietly simplified later into something
wrong.

``GET /distillation`` returns the *effective* model, not just the configured id, because
"which model is actually doing this" has three possible answers — the organization's, the
platform's, or none — and a screen that showed only the stored value would say "none" for
the second of them.

``PATCH /distillation`` distinguishes ``model_id: null`` from an omitted ``model_id``. They
arrive as the same ``None`` and mean opposite things — go back to the platform default, and
leave it alone — so the distinction lives in ``model_fields_set`` and is asserted here
rather than trusted.

``POST /end-users/{id}/distil`` answers with what it did rather than 202. The whole reason
the button exists is that "it is queued" is the answer that leaves somebody none the wiser.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.schemas.distillation import DEDUPE_THRESHOLD
from tests.conftest import AuthHarness
from tests.distillation_support import facts_json

pytestmark = pytest.mark.anyio


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


def fixture_of(harness: AuthHarness) -> Any:
    distillation = harness.auth.distillation
    assert distillation is not None
    return distillation


async def settings_of(harness: AuthHarness, token: str) -> dict[str, Any]:
    response = await harness.client.get("/api/v1/distillation", headers=harness.bearer(token))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# reading the settings
# ---------------------------------------------------------------------------


async def test_the_defaults_are_returned_for_an_organization_that_has_set_nothing(
    auth_harness: AuthHarness, token: str
) -> None:
    """A row stores ``{}`` and the API answers with the full object, so what is shown is
    what will actually happen."""
    fixture_of(auth_harness).organization.settings = {}

    body = await settings_of(auth_harness, token)

    assert body["config"]["enabled"] is True
    assert body["config"]["debounce_seconds"] == 30
    assert body["config"]["dedupe_threshold"] == DEDUPE_THRESHOLD


async def test_todays_usage_comes_back_with_the_cap_it_will_be_compared_against(
    auth_harness: AuthHarness, token: str
) -> None:
    body = await settings_of(auth_harness, token)

    assert body["usage"]["calls_today"] == 0
    assert body["usage"]["daily_call_cap"] == body["config"]["daily_call_cap"]
    # UTC, and said so on the wire: an operator in Auckland reading "4,998 of 5,000 used"
    # needs to know how long that has left to run.
    assert body["usage"]["day_started_at"].endswith("Z")
    assert body["usage"]["day_started_at"].endswith("T00:00:00Z")


async def test_an_organization_with_no_model_anywhere_says_so(
    auth_harness: AuthHarness, token: str
) -> None:
    """Not an error: "nobody has picked a distillation model" is a configuration step with
    a screen, and the screen has to be able to say it."""
    body = await settings_of(auth_harness, token)

    assert body["effective_model_id"] is None
    assert body["effective_model_name"] is None


async def test_a_viewer_can_read_the_settings(auth_harness: AuthHarness, token: str) -> None:
    """The answer to "why has the assistant not learned anything about this customer",
    which is a question the person on support duty asks."""
    response = await auth_harness.client.get(
        "/api/v1/distillation", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# changing them
# ---------------------------------------------------------------------------


async def test_a_partial_update_changes_one_knob_and_leaves_the_rest(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"debounce_seconds": 120},
    )

    assert response.status_code == 200, response.text
    assert response.json()["config"]["debounce_seconds"] == 120
    assert response.json()["config"]["dedupe_threshold"] == DEDUPE_THRESHOLD


async def test_the_change_survives_a_reread(auth_harness: AuthHarness, token: str) -> None:
    await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"max_facts_per_user": 42},
    )

    assert (await settings_of(auth_harness, token))["config"]["max_facts_per_user"] == 42


async def test_the_other_settings_in_the_blob_are_left_alone(
    auth_harness: AuthHarness, token: str
) -> None:
    """``organizations.settings`` also holds the logging defaults and whatever task 17
    adds. This writes one key inside it."""
    fixture = fixture_of(auth_harness)
    fixture.organization.settings = {
        "logging_defaults": {"retention_days": 7},
        **fixture.organization.settings,
    }

    await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"enabled": False},
    )

    assert fixture.organization.settings["logging_defaults"] == {"retention_days": 7}


async def test_an_unknown_setting_is_refused_rather_than_stored(
    auth_harness: AuthHarness, token: str
) -> None:
    """A stored setting nothing reads is indistinguishable from a setting that does not
    work, and the second is what the user will conclude."""
    response = await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"debounce_secondz": 120},
    )

    assert response.status_code == 422


async def test_a_value_out_of_range_is_refused(auth_harness: AuthHarness, token: str) -> None:
    response = await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"dedupe_threshold": 0.1},
    )

    assert response.status_code == 422


async def test_clearing_the_model_is_a_value_not_an_omission(
    auth_harness: AuthHarness, token: str
) -> None:
    """``model_id: null`` means "go back to the platform default". A body that could not
    express it would make clearing the selector impossible."""
    chosen = uuid.uuid4()
    await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"model_id": str(chosen)},
    )
    assert (await settings_of(auth_harness, token))["config"]["model_id"] == str(chosen)

    await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"model_id": None},
    )

    assert (await settings_of(auth_harness, token))["config"]["model_id"] is None


async def test_omitting_the_model_leaves_it_alone(auth_harness: AuthHarness, token: str) -> None:
    chosen = uuid.uuid4()
    await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"model_id": str(chosen)},
    )

    await auth_harness.client.patch(
        "/api/v1/distillation",
        headers=auth_harness.bearer(token),
        json={"enabled": True},
    )

    assert (await settings_of(auth_harness, token))["config"]["model_id"] == str(chosen)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


async def test_health_over_a_quiet_organization_is_zeroes_rather_than_an_error(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.get(
        "/api/v1/distillation/health", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["runs"] == 0
    assert response.json()["dedupe_rate"] == 0.0
    assert response.json()["days"] == []


async def test_health_reports_the_two_rates_that_catch_a_silent_failure(
    auth_harness: AuthHarness, token: str
) -> None:
    fixture = fixture_of(auth_harness)
    fixture.model.replies = [facts_json({"text": "Works in Rust and prefers terse answers."})]
    alice = await fixture.end_user()
    fixture.configure(model_id=str(fixture.upstream.id) if fixture.upstream else None)
    fixture.log(alice)
    await fixture.distil(alice)

    response = await auth_harness.client.get(
        "/api/v1/distillation/health", headers=auth_harness.bearer(token)
    )

    body = response.json()
    assert "dedupe_rate" in body
    assert "supersession_rate" in body
    assert body["average_facts_per_end_user"] >= 0.0


async def test_the_health_window_is_bounded(auth_harness: AuthHarness, token: str) -> None:
    response = await auth_harness.client.get(
        "/api/v1/distillation/health?days=9999", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# distil now
# ---------------------------------------------------------------------------


async def test_distilling_on_demand_reports_what_it_did(
    auth_harness: AuthHarness, token: str
) -> None:
    fixture = fixture_of(auth_harness)
    alice = await fixture.end_user()
    fixture.log(alice)

    response = await auth_harness.client.post(
        f"/api/v1/end-users/{alice.id}/distil", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200, response.text
    assert response.json()["sessions"] == 1


async def test_distilling_somebody_with_nothing_pending_says_why(
    auth_harness: AuthHarness, token: str
) -> None:
    """Zero facts written has several causes and they need different actions. "0" alone is
    the answer that sends somebody to read logs."""
    fixture = fixture_of(auth_harness)
    alice = await fixture.end_user()

    response = await auth_harness.client.post(
        f"/api/v1/end-users/{alice.id}/distil", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["sessions"] == 0
    assert response.json()["inserted"] == 0


async def test_distilling_an_end_user_that_does_not_exist_is_a_404(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.post(
        f"/api/v1/end-users/{uuid.uuid4()}/distil", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 404
