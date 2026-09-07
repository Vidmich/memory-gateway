"""The cross-tenant regression net.

Task 04 asks for one module that, for every org-scoped endpoint, asserts a foreign-org id
answers **404** — and asks every later task to add its endpoints to it. So the interesting
part of this file is :data:`SCOPED_ENDPOINTS`: a table, not a pile of hand-written tests,
so extending it in task 05 is one line rather than a new copy of the same assertions.

404 and not 403, throughout. A 403 confirms the id exists, which turns any endpoint that
takes one into an oracle for enumerating another organization's resources. The correct
answer to "may I see this?" from outside the tenant is that there is no such thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.ids import uuid7
from tests.conftest import DirectoryHarness

#: Placeholders a template can use. They are spelled exactly like the FastAPI path
#: parameters so ``test_the_net_covers_every_scoped_route`` can compare the table against
#: the OpenAPI schema. Each resolves to something that genuinely exists but belongs to
#: *another* organization — a made-up id would pass these tests for the wrong reason.
FOREIGN_ORGANIZATION = "{organization_id}"
FOREIGN_MEMBER = "{member_id}"
FOREIGN_INVITATION = "{invitation_id}"
FOREIGN_MODEL = "{model_id}"

#: A model owned by nobody. It is *visible* to every organization (SPEC §5.3, the
#: global catalog), which is exactly why it needs rows of its own here: every write
#: route must still answer 404, or "you can see it" would quietly become "you can
#: change it".
GLOBAL_MODEL = "{global_model_id}"


@dataclass(frozen=True)
class ScopedEndpoint:
    """One org-scoped operation, and what to send it.

    Later tasks append here: task 05 adds ``/models/{id}``, task 06 ``/gateways/{id}``
    and ``/keys/{id}``, task 09 ``/connectors/{id}``, and so on.
    """

    method: str
    template: str
    body: dict[str, Any] | None = None
    #: Free text, shown in the test id, for endpoints whose purpose is not obvious.
    note: str = field(default="")

    def __str__(self) -> str:
        return f"{self.method} {self.template}"


SCOPED_ENDPOINTS: tuple[ScopedEndpoint, ...] = (
    ScopedEndpoint("GET", f"/api/v1/organizations/{FOREIGN_ORGANIZATION}"),
    ScopedEndpoint("PATCH", f"/api/v1/organizations/{FOREIGN_ORGANIZATION}", {"name": "Owned"}),
    ScopedEndpoint("GET", f"/api/v1/organizations/{FOREIGN_ORGANIZATION}/members"),
    ScopedEndpoint(
        "POST",
        f"/api/v1/organizations/{FOREIGN_ORGANIZATION}/invitations",
        {"email": "intruder@example.com", "role": "org_admin"},
    ),
    ScopedEndpoint("PATCH", f"/api/v1/members/{FOREIGN_MEMBER}", {"role": "org_viewer"}),
    ScopedEndpoint("DELETE", f"/api/v1/members/{FOREIGN_MEMBER}"),
    ScopedEndpoint("DELETE", f"/api/v1/invitations/{FOREIGN_INVITATION}"),
    ScopedEndpoint("POST", f"/api/v1/invitations/{FOREIGN_INVITATION}/resend"),
    ScopedEndpoint("GET", f"/api/v1/models/{FOREIGN_MODEL}"),
    ScopedEndpoint("PATCH", f"/api/v1/models/{FOREIGN_MODEL}", {"name": "owned"}),
    ScopedEndpoint("DELETE", f"/api/v1/models/{FOREIGN_MODEL}"),
    ScopedEndpoint("POST", f"/api/v1/models/{FOREIGN_MODEL}/test"),
)

#: The global catalog is readable by everyone, so a foreign-id test on ``GET`` would be
#: wrong — it is meant to succeed. These are the write routes, which must not.
GLOBAL_MODEL_ENDPOINTS: tuple[ScopedEndpoint, ...] = (
    ScopedEndpoint("PATCH", f"/api/v1/models/{GLOBAL_MODEL}", {"name": "owned"}),
    ScopedEndpoint("DELETE", f"/api/v1/models/{GLOBAL_MODEL}"),
    ScopedEndpoint("POST", f"/api/v1/models/{GLOBAL_MODEL}/test"),
)


async def foreign_ids(harness: DirectoryHarness) -> dict[str, str]:
    """Real ids, all belonging to Globex, as seen from Acme."""
    world = harness.world
    issued = await world.directory.invite(
        world.actor(world.globex_admin),
        world.globex.id,
        email="globex-invitee@example.com",
        role="org_member",
    )
    return {
        FOREIGN_ORGANIZATION: str(world.globex.id),
        FOREIGN_MEMBER: str(world.globex_admin.id),
        FOREIGN_INVITATION: str(issued.invitation.id),
        FOREIGN_MODEL: str(world.globex_model.id),
    }


@pytest.mark.parametrize("endpoint", SCOPED_ENDPOINTS, ids=str)
async def test_a_foreign_id_is_not_found(
    endpoint: ScopedEndpoint, directory: DirectoryHarness
) -> None:
    ids = await foreign_ids(directory)
    path = endpoint.template
    for placeholder, value in ids.items():
        path = path.replace(placeholder, value)

    response = await directory.as_user(
        directory.world.acme_admin, endpoint.method, path, json_body=endpoint.body
    )

    assert response.status_code == 404, f"{endpoint} -> {response.status_code} {response.text}"


@pytest.mark.parametrize("endpoint", SCOPED_ENDPOINTS, ids=str)
async def test_an_id_that_does_not_exist_answers_identically(
    endpoint: ScopedEndpoint, directory: DirectoryHarness
) -> None:
    """The whole point: "not yours" and "not there" have to be the same answer, or the
    difference between them is the leak."""
    path = endpoint.template
    for placeholder in (FOREIGN_ORGANIZATION, FOREIGN_MEMBER, FOREIGN_INVITATION, FOREIGN_MODEL):
        path = path.replace(placeholder, str(uuid7()))

    response = await directory.as_user(
        directory.world.acme_admin, endpoint.method, path, json_body=endpoint.body
    )

    assert response.status_code == 404


@pytest.mark.parametrize("endpoint", SCOPED_ENDPOINTS, ids=str)
async def test_the_table_is_not_quietly_wrong(endpoint: ScopedEndpoint) -> None:
    """A template with no placeholder would pass every test above by testing nothing."""
    assert any(
        placeholder in endpoint.template
        for placeholder in (FOREIGN_ORGANIZATION, FOREIGN_MEMBER, FOREIGN_INVITATION, FOREIGN_MODEL)
    )


async def test_the_net_covers_every_scoped_route(directory: DirectoryHarness) -> None:
    """Every ``/api/v1`` operation with a path parameter is either in the table above or
    named here as deliberately out of it. This is what keeps the net from rotting as
    tasks 05 through 17 add routes."""
    schema = directory.app.openapi()
    with_parameters = {
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        if path.startswith("/api/v1/") and "{" in path
        for method in operations
    }

    covered = {
        f"{endpoint.method} {endpoint.template}"
        for endpoint in SCOPED_ENDPOINTS
        # The global-catalog rows point at the same routes with a different placeholder.
    } | {
        f"{endpoint.method} {endpoint.template.replace(GLOBAL_MODEL, FOREIGN_MODEL)}"
        for endpoint in GLOBAL_MODEL_ENDPOINTS
    }
    exempt = {
        # Unauthenticated by design: the token in the path is the credential, and the
        # invitation it resolves to is what establishes the organization.
        "GET /api/v1/invitations/accept/{token}",
        "POST /api/v1/invitations/accept/{token}",
    }

    assert with_parameters - covered - exempt == set()


# ---------------------------------------------------------------------------
# the same isolation, from the other directions
# ---------------------------------------------------------------------------


async def test_a_list_never_includes_another_organization(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.acme_viewer, "GET", "/api/v1/organizations")

    assert response.status_code == 200
    assert [item["slug"] for item in response.json()["items"]] == ["acme"]


async def test_a_member_list_never_includes_another_organization(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/organizations/{world.acme.id}/members"
    )

    emails = {item["email"] for item in response.json()["items"]}
    assert world.globex_admin.email not in emails
    assert world.superadmin.email not in emails


async def test_an_invitation_list_never_includes_another_organization(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    await world.directory.invite(
        world.actor(world.globex_admin),
        world.globex.id,
        email="globex-invitee@example.com",
        role="org_member",
    )

    response = await directory.as_user(world.acme_admin, "GET", "/api/v1/invitations")

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_the_assume_header_does_nothing_for_an_org_user(
    directory: DirectoryHarness,
) -> None:
    """Ignored rather than refused: a 403 would tell an org admin the header exists and
    is worth attacking. Ignoring it simply gives them their own data."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", "/api/v1/organizations", assuming=world.globex.id
    )

    assert response.status_code == 200
    assert [item["slug"] for item in response.json()["items"]] == ["acme"]


async def test_the_assume_header_still_cannot_reach_a_foreign_row(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "GET",
        f"/api/v1/organizations/{world.globex.id}",
        assuming=world.globex.id,
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the global catalog: visible to all, writable by the platform alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", GLOBAL_MODEL_ENDPOINTS, ids=str)
async def test_an_org_user_cannot_write_a_global_model(
    endpoint: ScopedEndpoint, directory: DirectoryHarness
) -> None:
    """404, not 403.

    A 403 here would confirm that the id names a real model — which for the global
    catalog it does — but the shape of the answer has to match the rest of the net, or
    the difference becomes the thing worth probing.
    """
    path = endpoint.template.replace(GLOBAL_MODEL, str(directory.world.global_model.id))

    response = await directory.as_user(
        directory.world.acme_admin, endpoint.method, path, json_body=endpoint.body
    )

    assert response.status_code == 404, f"{endpoint} -> {response.status_code} {response.text}"


async def test_an_org_user_can_read_a_global_model(directory: DirectoryHarness) -> None:
    """The other half. If this ever starts failing, the isolation above has been bought
    by breaking the feature."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/models/{world.global_model.id}"
    )

    assert response.status_code == 200
    assert response.json()["editable"] is False


async def test_a_model_list_never_includes_another_organization(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    response = await directory.as_user(world.acme_admin, "GET", "/api/v1/models")

    names = {item["name"] for item in response.json()["items"]}
    assert names == {world.acme_model.name, world.global_model.name}
