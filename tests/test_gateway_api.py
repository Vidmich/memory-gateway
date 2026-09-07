"""Gateways and API keys over HTTP.

Same harness as the model tests: the real app, real routing, real tokens minted by
actually logging in, with only persistence in memory. A role check that passes only
because a test forged a convenient token cannot pass here.

:data:`MATRIX` is the interesting part. Gateways and keys are the first pair of resources
in this system with **different** capability gates — an ``org_member`` may configure an
endpoint but may not mint a credential for it (SPEC §5.2) — and the table is where that
distinction is legible instead of scattered across the route decorators.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.ids import uuid7
from tests.conftest import DirectoryHarness

NEW_GATEWAY: dict[str, Any] = {"name": "Support Bot", "slug": "acme-support"}


# ---------------------------------------------------------------------------
# the capability matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    method: str
    #: ``{gateway}`` and ``{key}`` are replaced with Acme's own ids.
    path: str
    body_key: str | None = None

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


BODIES: dict[str, dict[str, Any]] = {
    "create": NEW_GATEWAY,
    "rename": {"name": "Renamed"},
    "probe": {"message": "hello"},
    "key": {"name": "production"},
}

#: Which roles may reach each route. ``True`` means "not refused by the capability gate";
#: what happens after that is the service's business and is tested next door.
#:
#: The two key-writing rows are the reason this table exists. ``org_member`` has
#: ``resources:write`` and not ``keys:manage``, so it configures the endpoint and cannot
#: issue a bearer credential for it — the one place in this task where the two gates part.
MATRIX: list[tuple[Call, dict[str, bool]]] = [
    (
        Call("GET", "/api/v1/gateways"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": True},
    ),
    (
        Call("GET", "/api/v1/gateways/{gateway}"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": True},
    ),
    (
        Call("POST", "/api/v1/gateways", "create"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("PATCH", "/api/v1/gateways/{gateway}", "rename"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("DELETE", "/api/v1/gateways/{gateway}"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("POST", "/api/v1/gateways/{gateway}/test", "probe"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": False},
    ),
    (
        Call("GET", "/api/v1/gateways/{gateway}/keys"),
        {"superadmin": True, "acme_admin": True, "acme_member": True, "acme_viewer": True},
    ),
    (
        Call("POST", "/api/v1/gateways/{gateway}/keys", "key"),
        {"superadmin": True, "acme_admin": True, "acme_member": False, "acme_viewer": False},
    ),
    (
        Call("DELETE", "/api/v1/keys/{key}"),
        {"superadmin": True, "acme_admin": True, "acme_member": False, "acme_viewer": False},
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
    path = call.path.replace("{gateway}", str(world.acme_gateway.id)).replace(
        "{key}", str(world.acme_key.id)
    )

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
# creating and reading
# ---------------------------------------------------------------------------


async def test_a_new_gateway_comes_back_with_its_endpoint_url(
    directory: DirectoryHarness,
) -> None:
    """The demo's first moment: a URL to copy, produced by the save."""
    response = await directory.as_user(
        directory.world.acme_admin, "POST", "/api/v1/gateways", json_body=NEW_GATEWAY
    )

    assert response.status_code == 201
    assert response.json()["endpoint_url"].endswith("/g/acme-support/v1")


async def test_the_response_carries_complete_config_defaults(
    directory: DirectoryHarness,
) -> None:
    """A gateway created today stores ``{}`` and answers with the full object, so the API
    shows what will happen rather than an empty blob the reader has to know defaults for."""
    response = await directory.as_user(
        directory.world.acme_admin, "POST", "/api/v1/gateways", json_body=NEW_GATEWAY
    )
    body = response.json()

    assert body["memory_config"]["doc_top_k"] == 6
    assert body["logging_config"]["enable_distillation"] is True
    assert body["limits"]["requests_per_minute"] is None


async def test_a_gateway_lists_its_target_model(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}"
    )

    assert [target["name"] for target in response.json()["targets"]] == ["acme-gpt"]


async def test_a_target_summary_carries_no_credential_status(
    directory: DirectoryHarness,
) -> None:
    """Deliberately not the full model response: the gateway editor must not become a
    second place a credential hint is rendered."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}"
    )

    assert "credential" not in response.json()["targets"][0]


async def test_a_duplicate_slug_is_a_409_naming_the_field(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/gateways",
        json_body={**NEW_GATEWAY, "slug": "globex-chat"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["param"] == "slug"


async def test_a_reserved_slug_is_refused(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/gateways",
        json_body={**NEW_GATEWAY, "slug": "admin"},
    )

    assert response.status_code == 422


async def test_a_malformed_slug_is_refused_before_the_service_sees_it(
    directory: DirectoryHarness,
) -> None:
    """The pattern on the schema gives the client a rule it can apply as you type; the
    service's sentence is what explains a value that got past it."""
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/gateways",
        json_body={**NEW_GATEWAY, "slug": "Not A Slug"},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# the slug is immutable
# ---------------------------------------------------------------------------


async def test_patching_the_slug_is_refused_with_a_reason(directory: DirectoryHarness) -> None:
    """Not "unexpected field" — the reason, because this is the rule people will argue
    with and the answer has to be in the response."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/gateways/{world.acme_gateway.id}",
        json_body={"slug": "renamed"},
    )

    assert response.status_code == 422
    assert "endpoint URL" in response.text


async def test_the_slug_survives_an_otherwise_valid_patch(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/gateways/{world.acme_gateway.id}",
        json_body={"name": "Renamed"},
    )

    assert response.json()["slug"] == "acme-chat"


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


async def test_a_key_is_revealed_exactly_once(directory: DirectoryHarness) -> None:
    world = directory.world
    created = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/gateways/{world.acme_gateway.id}/keys",
        json_body={"name": "production"},
    )
    token = created.json()["token"]

    listed = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/keys"
    )

    assert created.status_code == 201 and token.startswith("mg_")
    # Searching the serialized body rather than named fields, so a field added in a later
    # task is covered without anyone remembering to extend this.
    assert token not in listed.text


async def test_no_other_response_ever_carries_the_token(directory: DirectoryHarness) -> None:
    world = directory.world
    created = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/gateways/{world.acme_gateway.id}/keys",
        json_body={"name": "production"},
    )
    token = created.json()["token"]
    key_id = created.json()["key"]["id"]

    responses = [
        await directory.as_user(world.acme_admin, "GET", "/api/v1/gateways"),
        await directory.as_user(
            world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}"
        ),
        await directory.as_user(
            world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/keys"
        ),
        await directory.as_user(world.acme_admin, "DELETE", f"/api/v1/keys/{key_id}"),
    ]

    for response in responses:
        assert token not in response.text


async def test_a_key_listing_shows_the_prefix(directory: DirectoryHarness) -> None:
    """The durable display form. It carries no secret and is what a customer compares
    against the key in their own config when deciding which one to revoke."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/keys"
    )

    assert response.json()[0]["prefix"].startswith("mg_")


async def test_revoking_a_key_returns_it_stamped(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "DELETE", f"/api/v1/keys/{world.acme_key.id}"
    )

    assert response.status_code == 200
    assert response.json()["revoked_at"] is not None


async def test_a_revoked_key_is_still_listed(directory: DirectoryHarness) -> None:
    world = directory.world
    await directory.as_user(world.acme_admin, "DELETE", f"/api/v1/keys/{world.acme_key.id}")
    listed = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/keys"
    )

    assert [key["id"] for key in listed.json()] == [str(world.acme_key.id)]


async def test_an_expiry_is_accepted_and_returned(directory: DirectoryHarness) -> None:
    world = directory.world
    when = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    response = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/gateways/{world.acme_gateway.id}/keys",
        json_body={"name": "temporary", "expires_at": when},
    )

    assert response.status_code == 201
    assert response.json()["key"]["expires_at"] is not None


async def test_a_key_id_that_does_not_exist_is_a_404(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "DELETE", f"/api/v1/keys/{uuid7()}"
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


async def test_testing_a_gateway_returns_the_assembled_prompt(
    directory: DirectoryHarness,
) -> None:
    """The part that makes this a debugging tool rather than a health check: it is the
    only way to see what the system context became before task 07's log exists."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/gateways/{world.acme_gateway.id}/test",
        json_body={"message": "hello"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["assembled_prompt"][0]["role"] == "user"
    assert body["total_ms"] >= 0


async def test_an_empty_probe_message_is_refused(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/gateways/{world.acme_gateway.id}/test",
        json_body={"message": ""},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# config sections
# ---------------------------------------------------------------------------


async def test_a_config_section_is_merged_not_replaced(directory: DirectoryHarness) -> None:
    world = directory.world
    path = f"/api/v1/gateways/{world.acme_gateway.id}"
    await directory.as_user(
        world.acme_admin, "PATCH", path, json_body={"logging_config": {"retention_days": 7}}
    )
    response = await directory.as_user(
        world.acme_admin, "PATCH", path, json_body={"logging_config": {"log_response_body": False}}
    )
    logging_config = response.json()["logging_config"]

    assert logging_config["retention_days"] == 7
    assert logging_config["log_response_body"] is False


async def test_an_unknown_config_key_names_itself(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/gateways/{world.acme_gateway.id}",
        json_body={"memory_config": {"doc_top_kk": 8}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == "memory_config.doc_top_kk"


async def test_distillation_without_body_logging_is_refused(
    directory: DirectoryHarness,
) -> None:
    """SPEC §10.2: distillation reads transcripts. Accepting the pair would produce a
    gateway whose screen promises memory it can never build."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/gateways/{world.acme_gateway.id}",
        json_body={"logging_config": {"log_request_body": False}},
    )

    assert response.status_code == 422
    assert "distillation" in response.text


async def test_a_broken_redaction_pattern_is_refused(directory: DirectoryHarness) -> None:
    """Compiled at write time, so a bad regex is a 422 on the form rather than an
    exception on the logging path of somebody's live traffic."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/gateways/{world.acme_gateway.id}",
        json_body={"logging_config": {"redaction_patterns": ["(unclosed"]}},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# platform scope
# ---------------------------------------------------------------------------


async def test_a_superadmin_at_platform_scope_cannot_create_a_gateway(
    directory: DirectoryHarness,
) -> None:
    """There is no organization to create it in. Opening one first is the whole point of
    the assume header — and this is a 403 with that sentence, not a mysterious 500."""
    response = await directory.as_user(
        directory.world.superadmin, "POST", "/api/v1/gateways", json_body=NEW_GATEWAY
    )

    assert response.status_code == 403


async def test_a_superadmin_can_create_one_inside_an_organization(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(
        world.superadmin, "POST", "/api/v1/gateways", json_body=NEW_GATEWAY, assuming=world.acme.id
    )

    assert response.status_code == 201
    assert response.json()["organization_id"] == str(world.acme.id)


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


async def test_the_list_pages_with_a_cursor(directory: DirectoryHarness) -> None:
    world = directory.world
    for index in range(3):
        await directory.as_user(
            world.acme_admin,
            "POST",
            "/api/v1/gateways",
            json_body={"name": f"Gateway {index}", "slug": f"acme-{index}"},
        )

    first = await directory.as_user(world.acme_admin, "GET", "/api/v1/gateways?limit=2")
    cursor = first.json()["next_cursor"]
    second = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways?limit=2&cursor={cursor}"
    )

    first_ids = {item["id"] for item in first.json()["items"]}
    second_ids = {item["id"] for item in second.json()["items"]}
    assert len(first_ids) == 2
    assert first_ids & second_ids == set()
