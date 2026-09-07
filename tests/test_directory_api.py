"""The directory over HTTP: capabilities, role enforcement, and the acceptance flow.

Tokens here are minted by really logging in, so a check that only passes because a test
forged a convenient token cannot pass here.

The role table below is the API-level counterpart of ``tests/test_permissions.py``: that
one asserts the matrix is right, this one asserts the endpoints are wired to it. Both are
needed — a correct matrix that no route consults protects nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.api.control.auth import REFRESH_COOKIE
from tests.conftest import DirectoryHarness

ROLES = ("org_admin", "org_member", "org_viewer")


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


async def test_me_reports_the_capability_set(directory: DirectoryHarness) -> None:
    """One source of truth for the UI: it hides what this list does not contain, and the
    API rejects the same calls regardless (see the role table below)."""
    response = await directory.as_user(directory.world.acme_admin, "GET", "/api/v1/auth/me")

    body = response.json()
    assert body["role"] == "org_admin"
    assert set(body["capabilities"]) == {
        "org:read",
        "resources:write",
        "keys:manage",
        "org:administer",
    }


async def test_a_viewer_gets_only_read(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.acme_viewer, "GET", "/api/v1/auth/me")

    assert response.json()["capabilities"] == ["org:read"]


async def test_a_superadmin_gets_everything(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.superadmin, "GET", "/api/v1/auth/me")

    body = response.json()
    assert "platform:administer" in body["capabilities"]
    assert body["organization"] is None


async def test_me_names_the_organization(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.acme_member, "GET", "/api/v1/auth/me")

    organization = response.json()["organization"]
    assert organization["slug"] == "acme"
    assert organization["status"] == "active"


# ---------------------------------------------------------------------------
# the role matrix, at the endpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


#: One row per call. "Allowed" means the request is not refused for lack of permission;
#: it may still fail on a guard, which is why the assertion below is only about 403.
#: A list rather than a dict because a `Call` carries a body, and a body is not hashable.
MATRIX: list[tuple[Call, dict[str, bool]]] = [
    (
        Call("GET", "/api/v1/organizations"),
        {"org_admin": True, "org_member": True, "org_viewer": True},
    ),
    (
        Call("POST", "/api/v1/organizations", {"name": "New", "slug": "new-org"}),
        {"org_admin": False, "org_member": False, "org_viewer": False},
    ),
    (
        Call("GET", "/api/v1/invitations"),
        {"org_admin": True, "org_member": False, "org_viewer": False},
    ),
    (
        Call("GET", "/api/v1/auth/me"),
        {"org_admin": True, "org_member": True, "org_viewer": True},
    ),
]


@pytest.mark.parametrize(
    ("call", "role", "allowed"),
    [(call, role, allowed) for call, row in MATRIX for role, allowed in row.items()],
    ids=lambda value: str(value),
)
async def test_the_endpoints_consult_the_matrix(
    call: Call, role: str, allowed: bool, directory: DirectoryHarness
) -> None:
    user = {
        "org_admin": directory.world.acme_admin,
        "org_member": directory.world.acme_member,
        "org_viewer": directory.world.acme_viewer,
    }[role]

    response = await directory.as_user(user, call.method, call.path, json_body=call.body)

    if allowed:
        assert response.status_code != 403, response.text
    else:
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("role", ["org_member", "org_viewer"])
async def test_a_non_admin_cannot_change_a_member(role: str, directory: DirectoryHarness) -> None:
    world = directory.world
    user = world.acme_member if role == "org_member" else world.acme_viewer

    response = await directory.as_user(
        user, "PATCH", f"/api/v1/members/{world.acme_viewer.id}", json_body={"role": "org_admin"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("role", ["org_member", "org_viewer"])
async def test_a_non_admin_cannot_invite(role: str, directory: DirectoryHarness) -> None:
    world = directory.world
    user = world.acme_member if role == "org_member" else world.acme_viewer

    response = await directory.as_user(
        user,
        "POST",
        f"/api/v1/organizations/{world.acme.id}/invitations",
        json_body={"email": "sneaky@example.com", "role": "org_admin"},
    )

    assert response.status_code == 403


async def test_every_role_can_read_the_member_list(directory: DirectoryHarness) -> None:
    """Seeing who your colleagues are is not an admin power. Changing them is."""
    world = directory.world
    for user in (world.acme_admin, world.acme_member, world.acme_viewer):
        response = await directory.as_user(
            user, "GET", f"/api/v1/organizations/{world.acme.id}/members"
        )
        assert response.status_code == 200, response.text


async def test_a_refusal_by_role_is_403_not_404(directory: DirectoryHarness) -> None:
    """Inside your own organization the resource is not hidden from you — you may simply
    not do this. Conflating the two would make every 404 ambiguous."""
    world = directory.world
    response = await directory.as_user(
        world.acme_viewer, "DELETE", f"/api/v1/members/{world.acme_member.id}"
    )

    assert response.status_code == 403


# -- what an org admin may not do -----------------------------------------


async def test_an_org_admin_cannot_create_an_organization(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin,
        "POST",
        "/api/v1/organizations",
        json_body={"name": "Mine", "slug": "mine"},
    )

    assert response.status_code == 403


async def test_an_org_admin_cannot_change_their_own_status(directory: DirectoryHarness) -> None:
    """An organization that could un-suspend itself would make suspension advisory."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/organizations/{world.acme.id}",
        json_body={"status": "active"},
    )

    assert response.status_code == 403


async def test_an_org_admin_can_edit_their_own_profile(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/organizations/{world.acme.id}",
        json_body={"name": "Acme Corporation", "settings": {"logging_default": "full"}},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Acme Corporation"
    assert response.json()["settings"] == {"logging_default": "full"}


async def test_settings_have_a_ceiling(directory: DirectoryHarness) -> None:
    """`settings` is otherwise an unbounded write primitive for any org admin."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/organizations/{world.acme.id}",
        json_body={"settings": {"blob": "x" * 20_000}},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# the platform area
# ---------------------------------------------------------------------------


async def test_a_superadmin_creates_and_suspends(directory: DirectoryHarness) -> None:
    created = await directory.as_user(
        directory.world.superadmin,
        "POST",
        "/api/v1/organizations",
        json_body={"name": "Initech", "slug": "initech"},
    )
    assert created.status_code == 201, created.text

    suspended = await directory.as_user(
        directory.world.superadmin,
        "PATCH",
        f"/api/v1/organizations/{created.json()['id']}",
        json_body={"status": "suspended"},
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"


async def test_a_bad_slug_is_rejected(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.superadmin,
        "POST",
        "/api/v1/organizations",
        json_body={"name": "Bad", "slug": "Not A Slug"},
    )

    assert response.status_code == 422


async def test_the_platform_list_carries_counts(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.superadmin, "GET", "/api/v1/organizations")

    counts = {item["slug"]: item["member_count"] for item in response.json()["items"]}
    assert counts == {"acme": 3, "globex": 1}


async def test_open_as_narrows_a_superadmin(directory: DirectoryHarness) -> None:
    """The "open as" action: same session, one organization, and a log line saying so."""
    world = directory.world
    response = await directory.as_user(
        world.superadmin,
        "GET",
        f"/api/v1/organizations/{world.globex.id}/members",
        assuming=world.globex.id,
    )

    assert response.status_code == 200
    assert [item["email"] for item in response.json()["items"]] == [world.globex_admin.email]


async def test_a_malformed_assume_header_is_refused(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.superadmin, "GET", "/api/v1/organizations", assuming="not-a-uuid"
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# invitation acceptance
# ---------------------------------------------------------------------------


async def issue_invitation(directory: DirectoryHarness, *, role: str = "org_member") -> str:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/organizations/{world.acme.id}/invitations",
        json_body={"email": "invitee@example.com", "role": role},
    )
    assert response.status_code == 201, response.text
    url: str = response.json()["accept_url"]
    return url.rsplit("/", 1)[-1]


async def test_the_link_points_at_the_ui(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "POST",
        f"/api/v1/organizations/{world.acme.id}/invitations",
        json_body={"email": "invitee@example.com", "role": "org_member"},
    )

    assert "/invitations/accept/" in response.json()["accept_url"]
    assert response.json()["invitation"]["status"] == "pending"


async def test_the_link_is_never_returned_again(directory: DirectoryHarness) -> None:
    """Only the hash is stored, so the list endpoint has no field that could leak it."""
    await issue_invitation(directory)
    response = await directory.as_user(directory.world.acme_admin, "GET", "/api/v1/invitations")

    item = response.json()["items"][0]
    assert "accept_url" not in item
    assert "token" not in item and "token_hash" not in item


async def test_a_link_can_be_previewed_without_a_session(
    directory: DirectoryHarness,
) -> None:
    token = await issue_invitation(directory, role="org_viewer")
    response = await directory.client.get(f"/api/v1/invitations/accept/{token}")

    assert response.status_code == 200
    assert response.json() == {
        "email": "invitee@example.com",
        "role": "org_viewer",
        "organization_name": "Acme",
        "expires_at": response.json()["expires_at"],
    }


async def test_a_preview_reveals_no_ids(directory: DirectoryHarness) -> None:
    """The page has to name the organization being joined; nothing else about it is
    anyone's business before they have accepted."""
    token = await issue_invitation(directory)
    body = await directory.client.get(f"/api/v1/invitations/accept/{token}")

    assert "organization_id" not in body.json()
    assert "id" not in body.json()


async def test_an_unknown_token_is_a_404(directory: DirectoryHarness) -> None:
    response = await directory.client.get("/api/v1/invitations/accept/nope")

    assert response.status_code == 404


async def test_accepting_signs_the_new_member_in(directory: DirectoryHarness) -> None:
    token = await issue_invitation(directory)

    response = await directory.client.post(
        f"/api/v1/invitations/accept/{token}",
        json={"name": "New Person", "password": "a-perfectly-fine-password"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "invitee@example.com"
    assert body["user"]["organization"]["slug"] == "acme"
    assert body["user"]["capabilities"] == ["org:read", "resources:write"]
    # The session is real: a refresh cookie is set and the access token works.
    assert REFRESH_COOKIE in response.cookies

    me = await directory.client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200


async def test_a_used_link_stops_working(directory: DirectoryHarness) -> None:
    token = await issue_invitation(directory)
    first = await directory.client.post(
        f"/api/v1/invitations/accept/{token}",
        json={"name": "First", "password": "a-perfectly-fine-password"},
    )
    assert first.status_code == 200

    second = await directory.client.post(
        f"/api/v1/invitations/accept/{token}",
        json={"name": "Second", "password": "a-perfectly-fine-password"},
    )
    assert second.status_code == 404


async def test_a_short_password_is_refused(directory: DirectoryHarness) -> None:
    token = await issue_invitation(directory)

    response = await directory.client.post(
        f"/api/v1/invitations/accept/{token}", json={"name": "New", "password": "short"}
    )

    assert response.status_code == 422


async def test_resending_replaces_the_link(directory: DirectoryHarness) -> None:
    world = directory.world
    token = await issue_invitation(directory)
    listed = await directory.as_user(world.acme_admin, "GET", "/api/v1/invitations")
    invitation_id = listed.json()["items"][0]["id"]

    resent = await directory.as_user(
        world.acme_admin, "POST", f"/api/v1/invitations/{invitation_id}/resend"
    )

    assert resent.status_code == 200
    new_token = resent.json()["accept_url"].rsplit("/", 1)[-1]
    assert new_token != token
    assert (await directory.client.get(f"/api/v1/invitations/accept/{token}")).status_code == 404
    assert (
        await directory.client.get(f"/api/v1/invitations/accept/{new_token}")
    ).status_code == 200


# ---------------------------------------------------------------------------
# suspension
# ---------------------------------------------------------------------------


async def suspend_acme(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.superadmin,
        "PATCH",
        f"/api/v1/organizations/{world.acme.id}",
        json_body={"status": "suspended"},
    )
    assert response.status_code == 200


async def test_a_suspended_organization_cannot_sign_in(directory: DirectoryHarness) -> None:
    token = await directory.token_for(directory.world.acme_admin)
    assert token

    await suspend_acme(directory)

    response = await directory.client.post(
        "/api/v1/auth/login",
        json={
            "email": directory.world.acme_admin.email,
            "password": "correct-horse-battery-staple",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "organization_suspended"


async def test_suspension_ends_sessions_that_are_already_open(
    directory: DirectoryHarness,
) -> None:
    """Checked on every request, not only at login — otherwise suspending an organization
    would take up to a full access-token TTL to mean anything."""
    headers = await directory.headers_for(directory.world.acme_admin)
    assert (await directory.client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    await suspend_acme(directory)

    assert (await directory.client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_a_superadmin_is_unaffected(directory: DirectoryHarness) -> None:
    """Someone has to be able to un-suspend it."""
    headers = await directory.headers_for(directory.world.superadmin)
    await suspend_acme(directory)

    assert (await directory.client.get("/api/v1/auth/me", headers=headers)).status_code == 200


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


async def test_a_list_answers_in_the_documented_shape(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.superadmin, "GET", "/api/v1/organizations")

    assert set(response.json()) == {"items", "next_cursor"}


async def test_a_cursor_walks_the_whole_list(directory: DirectoryHarness) -> None:
    world = directory.world
    first = await directory.as_user(world.superadmin, "GET", "/api/v1/organizations?limit=1")
    assert first.json()["next_cursor"] is not None

    second = await directory.as_user(
        world.superadmin,
        "GET",
        f"/api/v1/organizations?limit=1&cursor={first.json()['next_cursor']}",
    )

    seen = [first.json()["items"][0]["slug"], second.json()["items"][0]["slug"]]
    assert sorted(seen) == ["acme", "globex"]
    assert second.json()["next_cursor"] is None


async def test_a_malformed_cursor_is_refused(directory: DirectoryHarness) -> None:
    """Silently returning page one when the client asked for page four is the kind of
    failure that gets diagnosed as data loss."""
    response = await directory.as_user(
        directory.world.superadmin, "GET", "/api/v1/organizations?cursor=garbage"
    )

    assert response.status_code == 422
