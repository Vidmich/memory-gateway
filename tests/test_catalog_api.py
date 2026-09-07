"""The model catalog over HTTP.

Same harness as the directory tests: the real app, real routing, real tokens minted by
actually logging in, with only persistence in memory. A role check that passes only
because a test forged a convenient token cannot pass here.

Two groups are worth reading. :data:`MATRIX` is the capability matrix expressed as data,
so "each capability x each role" is a table rather than twenty near-identical tests. And
the leak group searches *serialized responses* for a known plaintext, which is the only
form of that check that still works after somebody adds a field in task 09.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.core.ids import uuid7
from tests.catalog_support import ACME_SECRET, PLATFORM_SECRET
from tests.conftest import DirectoryHarness

NEW_MODEL: dict[str, Any] = {
    "name": "brand-new",
    "base_url": "https://api.example.com/v1",
    "upstream_model_id": "gpt-4o-mini",
    "credential": "sk-typed-into-the-form",
}


# ---------------------------------------------------------------------------
# the capability matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    #: ``{model}`` is replaced with Acme's own model id.
    body_key: str | None = None

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


BODIES: dict[str, dict[str, Any]] = {
    "create": NEW_MODEL,
    "rename": {"name": "renamed"},
}

#: Which roles may reach each route. ``True`` means "not refused by the capability gate";
#: what happens after that is the service's business and is tested next door.
#:
#: The superadmin column is exercised with an organization opened, because that is how a
#: platform account works on org-scoped resources — at bare platform scope there is no
#: organization to write into, which is a different refusal and has its own test below.
MATRIX: list[tuple[Call, dict[str, bool]]] = [
    (
        Call("GET", "/api/v1/models"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": True},
    ),
    (
        Call("GET", "/api/v1/models/{model}"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": True},
    ),
    (
        Call("POST", "/api/v1/models", "create"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("PATCH", "/api/v1/models/{model}", "rename"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("DELETE", "/api/v1/models/{model}"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("POST", "/api/v1/models/{model}/test"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("POST", "/api/v1/models/test", "create"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
]


def _cases() -> list[tuple[Call, str, bool]]:
    return [(call, role, may) for call, roles in MATRIX for role, may in roles.items()]


@pytest.mark.parametrize(
    ("call", "role", "may"), _cases(), ids=lambda value: str(value) if value is not True else "may"
)
async def test_the_capability_matrix(
    call: Call, role: str, may: bool, directory: DirectoryHarness
) -> None:
    world = directory.world
    user = world.superadmin if role == "superadmin" else world.people[role]
    path = call.path.replace("{model}", str(world.acme_model.id))

    response = await directory.as_user(
        user,
        call.method,
        path,
        json_body=BODIES.get(call.body_key or ""),
        assuming=world.acme.id if role == "superadmin" else None,
    )

    refused = response.status_code == 403
    assert refused is not may, f"{call} as {role} -> {response.status_code} {response.text}"


async def test_every_role_in_the_matrix_exists(directory: DirectoryHarness) -> None:
    """A typo in a role name would make its whole column silently vacuous."""
    named = {role for _, roles in MATRIX for role in roles} - {"superadmin"}

    assert named <= set(directory.world.people)


# ---------------------------------------------------------------------------
# no response carries a credential
# ---------------------------------------------------------------------------


async def test_no_response_contains_a_stored_credential(directory: DirectoryHarness) -> None:
    """Searches serialized bodies for the known plaintext rather than checking named
    fields, so a field added later is covered without anyone remembering to add it."""
    world = directory.world
    admin = world.acme_admin
    model = str(world.acme_model.id)

    responses = [
        await directory.as_user(admin, "GET", "/api/v1/models"),
        await directory.as_user(admin, "GET", f"/api/v1/models/{model}"),
        await directory.as_user(admin, "PATCH", f"/api/v1/models/{model}", json_body={"n": 1}),
        await directory.as_user(
            admin, "PATCH", f"/api/v1/models/{model}", json_body={"enabled": True}
        ),
        await directory.as_user(admin, "POST", f"/api/v1/models/{model}/test"),
        await directory.as_user(admin, "DELETE", f"/api/v1/models/{model}"),
    ]

    for response in responses:
        assert ACME_SECRET not in response.text, response.text


async def test_the_superadmin_cannot_read_a_credential_either(directory: DirectoryHarness) -> None:
    """SPEC §5.4 says there is no reveal endpoint *for any role*, and that includes the
    one role that could otherwise argue it should have one."""
    world = directory.world
    model = str(world.global_model.id)

    for call in (
        await directory.as_user(world.superadmin, "GET", f"/api/v1/models/{model}"),
        await directory.as_user(world.superadmin, "GET", "/api/v1/models"),
        await directory.as_user(world.superadmin, "POST", f"/api/v1/models/{model}/test"),
    ):
        assert PLATFORM_SECRET not in call.text


async def test_a_hint_is_shown_to_the_owner(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/models/{world.acme_model.id}"
    )

    assert response.json()["credential"] == {"configured": True, "hint": "sk-...pear"}


async def test_a_global_models_hint_is_withheld_from_a_tenant(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/models/{world.global_model.id}"
    )

    body = response.json()
    assert body["credential"] == {"configured": True, "hint": None}
    assert body["extra_headers"] == {}
    assert "operator-only-value" not in response.text


# ---------------------------------------------------------------------------
# listing and filtering
# ---------------------------------------------------------------------------


async def test_the_global_tab_is_a_filter_on_the_same_list(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(world.acme_admin, "GET", "/api/v1/models?scope=global")

    assert [item["name"] for item in response.json()["items"]] == ["shared-gpt-4o"]


async def test_the_our_models_tab_excludes_the_catalog(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", "/api/v1/models?scope=org"
    )

    assert [item["name"] for item in response.json()["items"]] == ["acme-gpt"]


async def test_an_unknown_scope_filter_is_rejected(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", "/api/v1/models?scope=everything"
    )

    assert response.status_code == 422


async def test_a_page_carries_a_cursor(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.superadmin, "GET", "/api/v1/models?limit=1")

    body = response.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"]


async def test_a_garbage_cursor_is_rejected(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", "/api/v1/models?cursor=not-a-uuid"
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# creating and editing
# ---------------------------------------------------------------------------


async def test_creating_returns_201_and_the_hint(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "POST", "/api/v1/models", json_body=NEW_MODEL
    )

    assert response.status_code == 201
    assert response.json()["credential"]["configured"] is True
    assert response.json()["editable"] is True


async def test_an_unknown_field_is_refused(directory: DirectoryHarness) -> None:
    """``extra="forbid"``. A typo'd field name that is quietly ignored produces a model
    that is not what the operator configured."""
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/models",
        json_body={**NEW_MODEL, "base_urls": "https://example.com"},
    )

    assert response.status_code == 422


async def test_a_base_url_must_be_http(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/models",
        json_body={**NEW_MODEL, "base_url": "api.example.com/v1"},
    )

    assert response.status_code == 422


async def test_a_trailing_slash_is_normalised_away(directory: DirectoryHarness) -> None:
    """``.../v1/`` plus ``/chat/completions`` is a 404 at most providers, and one that
    reads like a wrong model name."""
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/models",
        json_body={**NEW_MODEL, "base_url": "https://api.example.com/v1/"},
    )

    assert response.json()["base_url"] == "https://api.example.com/v1"


async def test_a_patch_without_a_credential_keeps_it(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/models/{world.acme_model.id}",
        json_body={"description": "now with a description"},
    )

    assert response.json()["credential"] == {"configured": True, "hint": "sk-...pear"}


async def test_a_patch_with_a_null_credential_clears_it(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/models/{world.acme_model.id}",
        json_body={"credential": None, "auth_type": "none"},
    )

    assert response.json()["credential"] == {"configured": False, "hint": None}


async def test_a_null_name_is_refused_rather_than_ignored(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "PATCH", f"/api/v1/models/{world.acme_model.id}", json_body={"name": None}
    )

    assert response.status_code == 422


async def test_a_bad_default_param_names_the_field(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/models/{world.acme_model.id}",
        json_body={"default_params": {"temperature": 11}},
    )

    assert response.status_code == 422
    assert "between 0 and 2" in response.json()["error"]["message"]


# ---------------------------------------------------------------------------
# the global catalog
# ---------------------------------------------------------------------------


async def test_an_org_user_creating_a_global_model_is_forbidden(
    directory: DirectoryHarness,
) -> None:
    """403, not 404: nothing is hidden, they simply may not write there."""
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/models",
        json_body={**NEW_MODEL, "scope": "global"},
    )

    assert response.status_code == 403


async def test_a_superadmin_creates_a_global_model(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.superadmin,
        "POST",
        "/api/v1/models",
        json_body={**NEW_MODEL, "scope": "global"},
    )

    assert response.status_code == 201
    assert response.json()["organization_id"] is None


async def test_a_superadmin_without_an_organization_cannot_create_an_org_model(
    directory: DirectoryHarness,
) -> None:
    """No organization to stamp on the row. Opening one is the answer, and the next test
    is the same request with the header."""
    response = await directory.as_user(
        directory.world.superadmin, "POST", "/api/v1/models", json_body=NEW_MODEL
    )

    assert response.status_code == 403


async def test_opening_an_organization_lets_a_superadmin_create_in_it(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(
        world.superadmin, "POST", "/api/v1/models", json_body=NEW_MODEL, assuming=world.globex.id
    )

    assert response.status_code == 201
    assert response.json()["organization_id"] == str(world.globex.id)


# ---------------------------------------------------------------------------
# deleting
# ---------------------------------------------------------------------------


async def test_deleting_a_referenced_model_is_a_409_naming_the_gateway(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "DELETE", f"/api/v1/models/{world.acme_model.id}"
    )

    assert response.status_code == 409
    body = response.json()["error"]
    assert "Acme Chat" in body["message"]
    assert body["details"]["gateways"][0]["slug"] == "acme-chat"


async def test_deleting_an_unreferenced_model_is_204(directory: DirectoryHarness) -> None:
    created = await directory.as_user(
        directory.world.acme_admin, "POST", "/api/v1/models", json_body=NEW_MODEL
    )

    response = await directory.as_user(
        directory.world.acme_admin, "DELETE", f"/api/v1/models/{created.json()['id']}"
    )

    assert response.status_code == 204


async def test_deleting_something_that_does_not_exist_is_404(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "DELETE", f"/api/v1/models/{uuid7()}"
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# test connection
# ---------------------------------------------------------------------------


async def test_testing_a_stored_model_reports_latency(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "POST", f"/api/v1/models/{world.acme_model.id}/test"
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "latency_ms": 42,
        "upstream_status": None,
        "error_message": None,
        "model_echo": "upstream-model",
    }


async def test_testing_a_draft_needs_no_saved_model(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "POST", "/api/v1/models/test", json_body=NEW_MODEL
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_the_test_route_is_not_swallowed_as_a_model_id(
    directory: DirectoryHarness,
) -> None:
    """``/models/test`` has to be declared before ``/models/{model_id}`` or FastAPI tries
    to parse "test" as a UUID and answers 422 — which reads like the endpoint is broken."""
    response = await directory.as_user(
        directory.world.acme_admin, "POST", "/api/v1/models/test", json_body=NEW_MODEL
    )

    assert response.status_code != 422


async def test_a_failing_probe_is_a_200_carrying_the_upstream_error(
    directory: DirectoryHarness,
) -> None:
    """ "The provider said 401" is the successful answer to "does this work". Returning it
    as an error status would make the UI's failure path and the API's disagree."""
    from app.services.model_probe import ProbeResult

    directory.world.probe.result = ProbeResult(
        ok=False,
        latency_ms=87,
        upstream_status=401,
        error_message="invalid_api_key Incorrect API key provided.",
    )

    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        f"/api/v1/models/{directory.world.acme_model.id}/test",
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error_message"] == "invalid_api_key Incorrect API key provided."
