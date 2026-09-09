"""``/api/v1/platform`` over HTTP: who may reach it, and what a PATCH actually does.

Two things are worth driving through the API rather than through the services.

**The permission is blunt and it should stay that way.** Every route here is superadmin
only — there is no read-only audience for the embedding model or another tenant's deletion
schedule inside a customer — and the parametrised refusal test is what keeps a later route
from being added without one.

**Changing the embedding model is not a settings write.** It arrives as a PATCH and has to
become a reindex whose completion writes the setting; the response says both what changed
and what is now in flight. That translation is the most surprising thing in this task, so
it is asserted from the outside, where its surprisingness would otherwise be discovered.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import DirectoryHarness

API = "/api/v1"

#: Every route on the platform router, as the SPA calls them. Parametrised rather than
#: written out per test, because what is being asserted is a property of the *set*.
ROUTES = [
    ("GET", f"{API}/platform/settings", None),
    ("PATCH", f"{API}/platform/settings", {"storage": {"quota_bytes": 4096}}),
    ("GET", f"{API}/platform/maintenance", None),
    ("POST", f"{API}/platform/maintenance/partitions", {}),
    ("POST", f"{API}/platform/maintenance/retention", {}),
    ("POST", f"{API}/platform/maintenance/sweep", {"apply": False}),
    ("POST", f"{API}/platform/reindex", {"dry_run": True}),
    ("GET", f"{API}/platform/vector-backends", None),
]


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=lambda value: str(value))
async def test_an_organization_admin_is_refused(
    directory: DirectoryHarness, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """403, not 404. The route is not hidden from them — their role simply may not, and
    hiding it would make "your role cannot" indistinguishable from "it does not exist"."""
    response = await directory.as_user(directory.world.acme_admin, method, path, json_body=body)

    assert response.status_code == 403, response.text


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=lambda value: str(value))
async def test_a_superadmin_may(
    directory: DirectoryHarness, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await directory.as_user(directory.world.superadmin, method, path, json_body=body)

    assert response.status_code == 200, response.text


async def test_the_retention_ceilings_are_readable_inside_an_organization(
    directory: DirectoryHarness,
) -> None:
    """The one thing on this router a tenant may see: it explains why the retention they
    typed is not the retention being honoured."""
    response = await directory.as_user(
        directory.world.acme_viewer, "GET", f"{API}/retention-ceilings"
    )

    assert response.status_code == 200
    assert set(response.json()) == {"max_body_days", "max_metadata_days"}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


async def test_a_section_with_no_row_says_it_came_from_the_environment(
    directory: DirectoryHarness,
) -> None:
    """ "From the environment" and "set to the same value" look identical on screen and are
    not the same state: the first changes when a pod is redeployed."""
    response = await directory.as_user(
        directory.world.superadmin, "GET", f"{API}/platform/settings"
    )

    body = response.json()
    assert "retention" in body["from_environment"]
    assert body["attribution"] == []


async def test_a_patch_records_who_changed_it(directory: DirectoryHarness) -> None:
    superadmin = directory.world.superadmin
    await directory.as_user(
        superadmin,
        "PATCH",
        f"{API}/platform/settings",
        json_body={"retention": {"max_body_days": 30}},
    )

    body = (await directory.as_user(superadmin, "GET", f"{API}/platform/settings")).json()

    assert body["settings"]["retention"]["max_body_days"] == 30
    assert "retention" not in body["from_environment"]
    entry = next(item for item in body["attribution"] if item["key"] == "retention")
    assert entry["updated_by"] == str(superadmin.id)


async def test_an_unknown_key_is_a_422_naming_the_field(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.superadmin,
        "PATCH",
        f"{API}/platform/settings",
        json_body={"retention": {"max_body_dayz": 30}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "retention.max_body_dayz"


# ---------------------------------------------------------------------------
# the embedding change
# ---------------------------------------------------------------------------


async def test_changing_the_embedding_model_without_confirming_is_refused(
    directory: DirectoryHarness,
) -> None:
    """The confirmation is the model name retyped, not a boolean: a PATCH carrying
    ``{"confirm": true}`` is one somebody could send by copying an example, and this one
    costs a corpus of embeddings."""
    response = await directory.as_user(
        directory.world.superadmin,
        "PATCH",
        f"{API}/platform/settings",
        json_body={"embedding": {"name": "text-embedding-3-large", "dimension": 3072}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "confirm_reindex"


async def test_a_confirmed_change_starts_a_run_and_leaves_the_setting_alone(
    directory: DirectoryHarness,
) -> None:
    """The translation this whole flow exists for.

    Writing the setting now would make every ingestion between here and the swap embed
    with the new model and upsert into a collection of the old width, which Qdrant
    refuses. So the response shows the old model as current and the new one as pending.
    """
    response = await directory.as_user(
        directory.world.superadmin,
        "PATCH",
        f"{API}/platform/settings",
        json_body={
            "embedding": {"name": "text-embedding-3-large", "dimension": 3072},
            "confirm_reindex": "text-embedding-3-large",
        },
    )

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["settings"]["embedding"]["name"] != "text-embedding-3-large"
    assert body["pending_embedding"]["name"] == "text-embedding-3-large"
    assert body["reindex"]["status"] == "running"
    assert body["reindex"]["to_dimension"] == 3072


async def test_the_run_is_handed_to_the_worker(directory: DirectoryHarness) -> None:
    """Recorded *and* enqueued. A run row nobody picks up is a progress bar that never
    moves, and the reason would be invisible."""
    await directory.as_user(
        directory.world.superadmin,
        "PATCH",
        f"{API}/platform/settings",
        json_body={
            "embedding": {"name": "text-embedding-3-large", "dimension": 3072},
            "confirm_reindex": "text-embedding-3-large",
        },
    )

    submitted = directory.world.platform.queue.submitted
    assert [request.name for request in submitted] == ["reindex"]
    assert "run_id" in submitted[0].payload


async def test_a_provider_change_alone_is_an_ordinary_save(
    directory: DirectoryHarness,
) -> None:
    """Moving the same model to a different endpoint produces the same vectors. Forcing a
    re-embed of every corpus for that would be an absurd price for a routing change."""
    response = await directory.as_user(
        directory.world.superadmin,
        "PATCH",
        f"{API}/platform/settings",
        json_body={"embedding": {"provider": "openai"}},
    )

    body = response.json()
    assert body["settings"]["embedding"]["provider"] == "openai"
    assert body["reindex"] is None
    assert directory.world.platform.queue.submitted == []


async def test_a_second_reindex_while_one_is_running_is_a_409(
    directory: DirectoryHarness,
) -> None:
    superadmin = directory.world.superadmin
    await directory.as_user(superadmin, "POST", f"{API}/platform/reindex", json_body={})

    response = await directory.as_user(superadmin, "POST", f"{API}/platform/reindex", json_body={})

    assert response.status_code == 409


async def test_a_dry_run_starts_nothing(directory: DirectoryHarness) -> None:
    """What the confirmation dialog calls, so the cost an operator agrees to is the cost
    the server counted rather than a number the browser worked out for itself."""
    response = await directory.as_user(
        directory.world.superadmin,
        "POST",
        f"{API}/platform/reindex",
        json_body={"dry_run": True},
    )

    assert response.status_code == 200
    assert "points" in response.json()
    assert directory.world.platform.queue.submitted == []


async def test_a_connector_scoped_reindex_must_name_its_organization(
    directory: DirectoryHarness,
) -> None:
    response = await directory.as_user(
        directory.world.superadmin,
        "POST",
        f"{API}/platform/reindex",
        json_body={"connector_id": str(directory.world.acme_connector.id)},
    )

    assert response.status_code == 422


async def test_a_run_that_does_not_exist_is_a_404(directory: DirectoryHarness) -> None:
    from app.core.ids import uuid7

    response = await directory.as_user(
        directory.world.superadmin, "GET", f"{API}/platform/reindex/{uuid7()}"
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------


async def test_the_maintenance_screen_reports_the_runway(
    directory: DirectoryHarness,
) -> None:
    response = await directory.as_user(
        directory.world.superadmin, "GET", f"{API}/platform/maintenance"
    )

    body = response.json()
    assert {entry["table"] for entry in body["runway"]} == {"request_logs", "transcripts"}
    assert body["runway_threshold_days"] > 0


async def test_creating_partitions_now_reports_what_it_made(
    directory: DirectoryHarness,
) -> None:
    """Synchronous, unlike everything else here, because the reason somebody presses it is
    that the runway alert has fired — at which point "it is queued" is the wrong answer."""
    response = await directory.as_user(
        directory.world.superadmin, "POST", f"{API}/platform/maintenance/partitions", json_body={}
    )

    body = response.json()
    assert body["job"] == "partitions"
    assert body["status"] == "succeeded"
    assert body["report"]["created"]["request_logs"]


async def test_the_sweep_defaults_to_a_report(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.superadmin, "POST", f"{API}/platform/maintenance/sweep", json_body={}
    )

    assert response.json()["applied"] is False
    assert response.json()["deleted"] == 0


async def test_the_destructive_organization_pass_needs_the_word(
    directory: DirectoryHarness,
) -> None:
    """The same rule the sweeper's ``apply`` follows, for the same reason."""
    response = await directory.as_user(
        directory.world.superadmin, "POST", f"{API}/platform/organizations/purge", json_body={}
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "confirm"


# ---------------------------------------------------------------------------
# vector backends
# ---------------------------------------------------------------------------


async def test_the_enabled_backends_are_reported_and_no_url_is(
    directory: DirectoryHarness,
) -> None:
    """The whole reason an organization may name a backend is that naming one cannot reach
    a URL. If a connection detail ever appeared in this payload, a superadmin screen would
    be one form away from pointing a tenant's data at an arbitrary address."""
    response = await directory.as_user(
        directory.world.superadmin, "GET", f"{API}/platform/vector-backends"
    )

    body = response.json()
    assert body["enabled"] == ["qdrant"]
    assert body["default"] == "qdrant"
    assert set(body) == {"enabled", "default", "bindings"}
    assert "url" not in response.text.lower().replace("qdrant_url", "")


async def test_migrating_to_a_backend_that_is_not_enabled_is_a_422(
    directory: DirectoryHarness,
) -> None:
    """And the message names what is available, because the caller is a person choosing
    from a list rather than a program that guessed."""
    response = await directory.as_user(
        directory.world.superadmin,
        "POST",
        f"{API}/platform/organizations/{directory.world.acme.id}/vector-backend",
        json_body={"backend": "pinecone", "dry_run": True},
    )

    assert response.status_code == 422, response.text
    assert "qdrant" in response.text


async def test_migrating_to_where_it_already_is_is_refused(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.superadmin,
        "POST",
        f"{API}/platform/organizations/{directory.world.acme.id}/vector-backend",
        json_body={"backend": "qdrant", "dry_run": True},
    )

    assert response.status_code == 422, response.text


async def test_an_organization_admin_may_not_move_their_own_vectors(
    directory: DirectoryHarness,
) -> None:
    """It is a platform decision: it depends on which backends the *deployment* has, and on
    operational properties a tenant cannot see."""
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        f"{API}/platform/organizations/{directory.world.acme.id}/vector-backend",
        json_body={"backend": "chroma"},
    )

    assert response.status_code == 403, response.text
