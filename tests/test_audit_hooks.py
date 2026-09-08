"""Every mutating control-plane endpoint produces exactly one audit event.

This is the point of task 15. Audit coverage decays the moment somebody adds an endpoint,
and a log with gaps is worse than none because it is trusted — so the coverage is a test
rather than a review habit, in two halves that fail for different reasons.

:func:`test_every_mutating_route_is_declared` enumerates the real routing table and
insists that each mutating route is either in :data:`AUDITED`, with the action it must
produce, or in :data:`UNAUDITED`, with a sentence saying why it is not a mutation. A new
endpoint fails CI until its author decides which it is.

:func:`test_the_tour_records_every_declared_action` then *drives* the whole control plane
over HTTP and asserts that every action named in :data:`AUDITED` actually appeared. The
first half catches a route nobody thought about; the second catches a route that was
declared and then wired to a service method with no hook in it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import Response

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import AuditEvent
from app.main import create_app
from app.services.audit import Attribution
from tests.auth_support import PASSWORD
from tests.conftest import DirectoryHarness

API = "/api/v1"

#: Route to the audit action it must record. The right-hand side is what the contextual
#: history tabs and the action filter are built on, so a typo here is a filter that
#: silently matches nothing — which is why the tour below asserts each one really fires.
AUDITED: dict[tuple[str, str], str] = {
    ("POST", f"{API}/auth/password"): "user.password_change",
    ("POST", f"{API}/connectors"): "connector.create",
    ("PATCH", f"{API}/connectors/{{connector_id}}"): "connector.update",
    ("DELETE", f"{API}/connectors/{{connector_id}}"): "connector.delete",
    ("POST", f"{API}/connectors/{{connector_id}}/resync"): "connector.resync",
    ("POST", f"{API}/connectors/{{connector_id}}/upload"): "connector.upload",
    ("PATCH", f"{API}/distillation"): "organization.distillation.update",
    ("DELETE", f"{API}/documents/{{document_id}}"): "document.delete",
    ("POST", f"{API}/documents/{{document_id}}/reindex"): "document.reindex",
    ("POST", f"{API}/end-users/{{end_user_id}}/distil"): "end_user.memory.distil",
    ("POST", f"{API}/end-users/{{end_user_id}}/memory"): "memory_fact.create",
    ("DELETE", f"{API}/end-users/{{end_user_id}}/memory"): "end_user.memory.purge",
    ("POST", f"{API}/gateways"): "gateway.create",
    ("PATCH", f"{API}/gateways/{{gateway_id}}"): "gateway.update",
    ("DELETE", f"{API}/gateways/{{gateway_id}}"): "gateway.delete",
    ("POST", f"{API}/gateways/{{gateway_id}}/keys"): "key.create",
    ("DELETE", f"{API}/keys/{{key_id}}"): "key.revoke",
    ("POST", f"{API}/invitations/accept/{{token}}"): "invitation.accept",
    ("POST", f"{API}/invitations/{{invitation_id}}/resend"): "invitation.resend",
    ("DELETE", f"{API}/invitations/{{invitation_id}}"): "invitation.revoke",
    ("PATCH", f"{API}/members/{{member_id}}"): "member.update",
    ("DELETE", f"{API}/members/{{member_id}}"): "member.remove",
    ("PATCH", f"{API}/memory-facts/{{fact_id}}"): "memory_fact.update",
    ("DELETE", f"{API}/memory-facts/{{fact_id}}"): "memory_fact.delete",
    ("POST", f"{API}/models"): "model.create",
    ("PATCH", f"{API}/models/{{model_id}}"): "model.update",
    ("DELETE", f"{API}/models/{{model_id}}"): "model.delete",
    ("POST", f"{API}/organizations"): "organization.create",
    ("PATCH", f"{API}/organizations/{{organization_id}}"): "organization.update",
    ("POST", f"{API}/organizations/{{organization_id}}/invitations"): "invitation.create",
}

#: Routes with a mutating verb that change no configuration, and why. A reason rather
#: than a bare list: "this one is fine" is a claim, and the next person has to be able to
#: check it without re-deriving what the endpoint does.
UNAUDITED: dict[tuple[str, str], str] = {
    ("POST", f"{API}/auth/login"): (
        "A session, not a configuration change. Every login is already a row in "
        "`sessions` carrying its own address and user agent, which is where somebody "
        "investigating an account looks; duplicating it here would double the table's "
        "growth rate for no second answer."
    ),
    ("POST", f"{API}/auth/refresh"): (
        "Token rotation inside an existing session, recorded as a new `sessions` row "
        "in the same family; see login."
    ),
    ("POST", f"{API}/auth/logout"): (
        "Ends a session. The `sessions` row carries `revoked_reason`, which says more "
        "than an audit entry would."
    ),
    ("POST", f"{API}/connectors/{{connector_id}}/search"): (
        "A read that takes a body because the query is one. Nothing is written."
    ),
    ("POST", f"{API}/connectors/{{connector_id}}/upload-url"): (
        "Mints a presigned URL and writes nothing at all. What it authorises shows up "
        "as `connector.resync`, which is the reconciliation that has to exist anyway."
    ),
    ("POST", f"{API}/end-users/{{end_user_id}}/memory/search"): (
        "A similarity search over one person's facts. A read, for the same reason the "
        "connector search above is one: the body is the query."
    ),
    ("POST", f"{API}/gateways/{{gateway_id}}/try-retrieval"): (
        "The editor's retrieval preview. Retrieves, assembles, stores nothing."
    ),
    ("POST", f"{API}/gateways/{{gateway_id}}/prompt-preview"): (
        "Assembles the prompt for a sample question and shows it. Costs a retrieval and "
        "no tokens, and writes nothing."
    ),
    ("POST", f"{API}/gateways/{{gateway_id}}/test"): (
        "Sends a probe completion through the real proxy path, changing no "
        "configuration. It lands in the request log like any other request, which is "
        "where a probe belongs."
    ),
    ("POST", f"{API}/models/test"): (
        "Probes a draft that has not been saved. There is no row for it to change."
    ),
    ("POST", f"{API}/models/{{model_id}}/test"): (
        "Probes a stored model with its stored credential and stored URL, changing "
        "neither; see the model-draft probe above."
    ),
}

#: Actions with no route of their own, and where they come from instead. Named so the
#: tour's completeness check does not silently pass by forgetting them.
OFF_ROUTE = {
    "connector.purge": "the delete-connector job, once the row is actually gone",
    "organization.assume": "the first request of a support session, debounced",
}


def mutating_routes() -> set[tuple[str, str]]:
    """The control plane's mutating routes, read off the app that ships.

    Through the OpenAPI document rather than ``app.routes``: FastAPI resolves included
    routers lazily, so the route table is not a flat list, and the document is the same
    description the SPA's client is generated from.
    """
    spec = create_app().openapi()
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() in {"POST", "PATCH", "PUT", "DELETE"} and path.startswith(API)
    }


def test_every_mutating_route_is_declared() -> None:
    """The CI gate. A new endpoint has to say which kind it is."""
    declared = set(AUDITED) | set(UNAUDITED)
    undeclared = mutating_routes() - declared
    assert not undeclared, (
        "These mutating endpoints are not accounted for in tests/test_audit_hooks.py. "
        "Add an audit hook and list it in AUDITED, or list it in UNAUDITED with the "
        f"reason it changes no configuration: {sorted(undeclared)}"
    )


def test_no_declaration_outlives_its_route() -> None:
    """The other direction: a removed endpoint leaves a claim nothing checks."""
    stale = (set(AUDITED) | set(UNAUDITED)) - mutating_routes()
    assert not stale, f"These declarations name routes that no longer exist: {sorted(stale)}"


def test_every_unaudited_route_gives_a_reason() -> None:
    for route, reason in UNAUDITED.items():
        assert len(reason) > 30, route


# ---------------------------------------------------------------------------
# the tour
# ---------------------------------------------------------------------------


def ok(response: Response, *, expect: tuple[int, ...] = (200, 201, 204)) -> Any:
    assert response.status_code in expect, (
        f"{response.request.method} {response.request.url.path} -> "
        f"{response.status_code}: {response.text}"
    )
    return response.json() if response.content and response.status_code != 204 else None


async def tour(directory: DirectoryHarness) -> list[str]:
    """Drive one of everything, as the roles that are allowed to.

    Deliberately one long function rather than a test each: what is being proved is that
    the *set* of actions is complete, and thirty tests that each assert one action would
    let the thirty-first go missing without anything failing.

    Returns every secret it handled — two provider credentials, an API key token, two
    invitation links, two passwords — so the redaction check can grep for the real values
    rather than for something that looks like them.
    """
    secrets: list[str] = []
    world = directory.world
    admin, superadmin = world.acme_admin, world.superadmin
    acme = world.acme.id

    # -- organizations, members, invitations ------------------------------
    ok(
        await directory.as_user(
            superadmin,
            "POST",
            f"{API}/organizations",
            json_body={"name": "Initech", "slug": "initech"},
        )
    )
    ok(
        await directory.as_user(
            admin, "PATCH", f"{API}/organizations/{acme}", json_body={"name": "Acme Corp"}
        )
    )
    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/members/{world.acme_viewer.id}",
            json_body={"role": "org_member"},
        )
    )

    invitation = ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/organizations/{acme}/invitations",
            json_body={"email": "grace@acme.example.com", "role": "org_member"},
        )
    )
    secrets.append(invitation["accept_url"].rsplit("/", 1)[-1])
    invitation_id = invitation["invitation"]["id"]
    ok(await directory.as_user(admin, "POST", f"{API}/invitations/{invitation_id}/resend"))
    ok(
        await directory.as_user(admin, "DELETE", f"{API}/invitations/{invitation_id}"),
        expect=(204,),
    )

    accepted = ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/organizations/{acme}/invitations",
            json_body={"email": "hopper@acme.example.com", "role": "org_member"},
        )
    )
    token = accepted["accept_url"].rsplit("/", 1)[-1]
    secrets.append(token)
    ok(
        await directory.client.post(
            f"{API}/invitations/accept/{token}",
            json={"name": "Grace Hopper", "password": "another-correct-horse-battery"},
        )
    )
    ok(
        await directory.as_user(admin, "DELETE", f"{API}/members/{world.acme_member.id}"),
        expect=(204,),
    )

    # -- the model catalog -------------------------------------------------
    model = ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/models",
            json_body={
                "name": "audit-gpt",
                "base_url": "https://api.example.com/v1",
                "upstream_model_id": "gpt-4o",
                "credential": "sk-first-secret",
            },
        )
    )
    secrets.append("sk-first-secret")
    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/models/{model['id']}",
            json_body={"credential": "sk-second-secret", "timeout_seconds": 30},
        )
    )
    secrets.append("sk-second-secret")

    # -- gateways and keys -------------------------------------------------
    gateway = ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/gateways",
            json_body={
                "name": "Audit",
                "slug": "audit-tour",
                "targets": [{"model_id": model["id"]}],
            },
        )
    )
    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/gateways/{gateway['id']}",
            json_body={
                "system_context": "Answer from the handbook.",
                "memory_config": {"doc_top_k": 10},
            },
        )
    )
    key = ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/gateways/{gateway['id']}/keys",
            json_body={"name": "production"},
        )
    )
    secrets.append(key["token"])
    ok(await directory.as_user(admin, "DELETE", f"{API}/keys/{key['key']['id']}"))
    ok(
        await directory.as_user(admin, "DELETE", f"{API}/gateways/{gateway['id']}"),
        expect=(204,),
    )
    # Only now: a model a gateway points at cannot be deleted, which is the correct
    # refusal and would make this step fail for the right reason at the wrong time.
    ok(await directory.as_user(admin, "DELETE", f"{API}/models/{model['id']}"), expect=(204,))

    # -- connectors and documents -----------------------------------------
    connector = ok(
        await directory.as_user(
            admin, "POST", f"{API}/connectors", json_body={"name": "Audit handbook"}
        )
    )
    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/connectors/{connector['id']}",
            json_body={"chunking": {"chunk_size": 400}},
        )
    )
    ok(
        await directory.client.post(
            f"{API}/connectors/{connector['id']}/upload",
            headers=await directory.headers_for(admin),
            files=[("files", ("handbook.md", b"# Handbook\n\nBe kind.", "text/markdown"))],
        )
    )
    ok(await directory.as_user(admin, "POST", f"{API}/connectors/{connector['id']}/resync"))
    ok(await directory.as_user(admin, "POST", f"{API}/documents/{world.acme_document.id}/reindex"))
    ok(
        await directory.as_user(admin, "DELETE", f"{API}/documents/{world.acme_document.id}"),
        expect=(204,),
    )
    ok(
        await directory.as_user(admin, "DELETE", f"{API}/connectors/{connector['id']}"),
        expect=(202,),
    )

    # -- conversation memory ----------------------------------------------
    alice = world.acme_end_user.id
    fact = ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/end-users/{alice}/memory",
            json_body={"text": "Prefers replies in French.", "kind": "preference"},
        )
    )
    ok(
        await directory.as_user(
            admin, "PATCH", f"{API}/memory-facts/{fact['id']}", json_body={"confidence": 0.5}
        )
    )
    ok(
        await directory.as_user(admin, "DELETE", f"{API}/memory-facts/{fact['id']}"),
        expect=(204,),
    )
    ok(await directory.as_user(admin, "POST", f"{API}/end-users/{alice}/distil"))
    ok(await directory.as_user(admin, "DELETE", f"{API}/end-users/{alice}/memory"))
    ok(
        await directory.as_user(
            admin, "PATCH", f"{API}/distillation", json_body={"debounce_seconds": 120}
        )
    )

    # Last, deliberately: every step above signs in again, and changing the password
    # first would make the rest of the tour fail as an authentication problem.
    ok(
        await directory.as_user(
            admin,
            "POST",
            f"{API}/auth/password",
            json_body={
                "current_password": PASSWORD,
                "new_password": "a-different-correct-horse",
            },
        ),
        expect=(204,),
    )
    secrets.extend([PASSWORD, "a-different-correct-horse"])
    return secrets


async def test_the_tour_records_every_declared_action(directory: DirectoryHarness) -> None:
    await tour(directory)

    recorded = {row.action for row in directory.world.database.audit_events.values()}
    missing = set(AUDITED.values()) - recorded
    assert not missing, (
        "These routes are declared as audited but recorded nothing when driven. "
        f"The hook is missing or the action name does not match: {sorted(missing)}"
    )


async def test_the_tour_invents_no_actions_nobody_declared(
    directory: DirectoryHarness,
) -> None:
    """The other way round: an action the tour produces that no route claims is either a
    typo in a hook or a name that has quietly drifted from its declaration."""
    await tour(directory)

    recorded = {row.action for row in directory.world.database.audit_events.values()}
    unknown = recorded - set(AUDITED.values()) - set(OFF_ROUTE)
    assert not unknown, f"actions recorded by no declared route: {sorted(unknown)}"


async def test_every_event_names_its_actor_and_its_target(
    directory: DirectoryHarness,
) -> None:
    """A row with no actor or no target is a row nobody can act on."""
    await tour(directory)

    for event in directory.world.database.audit_events.values():
        assert event.actor_label, event.action
        assert event.target_type, event.action
        assert event.actor_type in {"user", "system", "superadmin_impersonation"}


async def test_no_secret_reaches_any_stored_diff(directory: DirectoryHarness) -> None:
    """The acceptance criterion, over the whole tour rather than one field at a time.

    The tour writes a model credential twice, mints an API key, mints two invitation
    links and changes a password. None of those values may appear anywhere in the table.

    A key's *prefix* is not among them and is deliberately stored: it is the display form
    every API response already carries, and it is what somebody matches against a client's
    configuration when deciding which key to revoke.
    """
    secrets = await tour(directory)
    assert len(secrets) >= 7

    payload = str([row.diff for row in directory.world.database.audit_events.values()])
    for secret in secrets:
        assert secret not in payload, secret


async def test_a_gateway_edit_diffs_by_readable_path(directory: DirectoryHarness) -> None:
    """The demo: a system-prompt change, rendered as a field-level before and after."""
    world = directory.world
    gateway = world.acme_gateway

    ok(
        await directory.as_user(
            world.acme_admin,
            "PATCH",
            f"{API}/gateways/{gateway.id}",
            json_body={
                "system_context": "You are a support assistant.",
                "memory_config": {"doc_top_k": 10},
            },
        )
    )

    event = _one(world.database.audit_events.values(), "gateway.update")
    changed = {change["path"]: change for change in event.diff["changes"]}
    assert changed["system_context"]["after"] == "You are a support assistant."
    assert changed["memory_config.doc_top_k"]["before"] == 6
    assert changed["memory_config.doc_top_k"]["after"] == 10
    assert event.target_label == gateway.slug


async def test_rotating_a_credential_says_that_it_changed_and_nothing_more(
    directory: DirectoryHarness,
) -> None:
    """``credential: "***" → "***"`` — the demo, exactly."""
    world = directory.world

    ok(
        await directory.as_user(
            world.acme_admin,
            "PATCH",
            f"{API}/models/{world.acme_model.id}",
            json_body={"credential": "sk-rotated-to-this"},
        )
    )

    event = _one(world.database.audit_events.values(), "model.update")
    assert event.diff["changes"] == [{"path": "credential", "before": "***", "after": "***"}]
    assert "sk-rotated-to-this" not in str(event.diff)


async def test_a_deleted_target_is_still_readable_by_its_label(
    directory: DirectoryHarness,
) -> None:
    """``target_label`` denormalises the name so the log survives its subject."""
    world = directory.world

    ok(
        await directory.as_user(
            world.acme_admin, "DELETE", f"{API}/members/{world.acme_viewer.id}"
        ),
        expect=(204,),
    )

    event = _one(world.database.audit_events.values(), "member.remove")
    assert world.acme_viewer.id not in world.database.users
    assert event.target_label == world.acme_viewer.email
    assert event.target_id == world.acme_viewer.id


async def test_a_bulk_upload_is_one_event_with_a_count(
    directory: DirectoryHarness,
) -> None:
    """Forty files dropped in is one thing somebody did, not forty rows."""
    world = directory.world

    ok(
        await directory.client.post(
            f"{API}/connectors/{world.acme_connector.id}/upload",
            headers=await directory.headers_for(world.acme_admin),
            files=[
                ("files", (f"note-{index}.md", b"# Note", "text/markdown")) for index in range(8)
            ],
        )
    )

    event = _one(world.database.audit_events.values(), "connector.upload")
    assert event.diff["summary"]["count"] == 8
    assert len(event.diff["summary"]["sample"]) == 5


async def test_support_access_is_recorded_in_the_customers_own_log(
    directory: DirectoryHarness,
) -> None:
    """SPEC §5.2, and the half a customer cares about: they can see it themselves."""
    world = directory.world

    response = await directory.as_user(
        world.superadmin, "GET", f"{API}/gateways", assuming=world.acme.id
    )
    assert response.status_code == 200

    event = _one(world.database.audit_events.values(), "organization.assume")
    assert event.organization_id == world.acme.id
    assert event.actor_type == "superadmin_impersonation"
    assert event.actor_label == world.superadmin.email


async def test_a_support_session_is_one_event_and_not_one_per_request(
    directory: DirectoryHarness,
) -> None:
    """Reading three screens is forty requests and one visit."""
    world = directory.world

    for _ in range(3):
        await directory.as_user(world.superadmin, "GET", f"{API}/gateways", assuming=world.acme.id)

    assert len(_all(world.database.audit_events.values(), "organization.assume")) == 1


async def test_an_ordinary_request_records_no_support_access(
    directory: DirectoryHarness,
) -> None:
    await directory.as_user(world_admin(directory), "GET", f"{API}/gateways")
    assert not _all(directory.world.database.audit_events.values(), "organization.assume")


async def test_a_platform_edit_inside_an_organization_is_marked_as_support(
    directory: DirectoryHarness,
) -> None:
    """No assume header at all — the route a call site would forget about.

    ``DirectoryService._narrow`` lets a superadmin at platform scope edit a member, and
    the attribution is derived from the fact that the event landed in an organization's
    log rather than from the caller remembering to say so.
    """
    world = directory.world

    ok(
        await directory.as_user(
            world.superadmin,
            "PATCH",
            f"{API}/members/{world.acme_viewer.id}",
            json_body={"status": "suspended"},
        )
    )

    event = _one(world.database.audit_events.values(), "member.update")
    assert event.actor_type == "superadmin_impersonation"
    assert event.organization_id == world.acme.id


async def test_a_platform_edit_of_a_customers_gateway_lands_in_their_log(
    directory: DirectoryHarness,
) -> None:
    """The organization comes from the *row*, never from the actor's scope.

    A platform administrator's scope has no organization at all, so an event that fell
    back on it would land in nobody's log — which is precisely the event a customer most
    wants to find in theirs.
    """
    world = directory.world

    ok(
        await directory.as_user(
            world.superadmin,
            "PATCH",
            f"{API}/gateways/{world.acme_gateway.id}",
            json_body={"name": "Renamed by support"},
        )
    )

    event = _one(world.database.audit_events.values(), "gateway.update")
    assert event.organization_id == world.acme.id
    assert event.actor_type == "superadmin_impersonation"


async def test_a_global_model_event_belongs_to_no_customer(
    directory: DirectoryHarness,
) -> None:
    """It is the platform's own record; an organization has no business seeing it."""
    world = directory.world

    ok(
        await directory.as_user(
            world.superadmin,
            "POST",
            f"{API}/models",
            json_body={
                "name": "shared-audit-model",
                "base_url": "https://api.example.com/v1",
                "upstream_model_id": "gpt-4o",
                "scope": "global",
            },
        )
    )

    event = _one(world.database.audit_events.values(), "model.create")
    assert event.organization_id is None
    assert event.actor_type == "user"


async def test_a_background_job_is_attributed_to_the_system(
    directory: DirectoryHarness,
) -> None:
    """Task 09's connector deletion, which removes the row long after the request ended."""
    world = directory.world
    connectors = world.auth.connectors
    assert connectors is not None

    await connectors.pipeline.purge(
        organization_id=world.acme.id, connector_id=world.acme_connector.id
    )

    event = _one(world.database.audit_events.values(), "connector.purge")
    assert event.actor_type == "system"
    assert event.actor_user_id is None
    assert event.actor_label == "delete-connector"
    assert event.organization_id == world.acme.id


# ---------------------------------------------------------------------------
# the demo, end to end
# ---------------------------------------------------------------------------


async def test_four_changes_produce_four_readable_entries(directory: DirectoryHarness) -> None:
    """Task 15's demo, through the API in both directions.

    Four unrelated changes, then the audit endpoint — not the store — asked what happened.
    This is the one test that closes the loop: everything else either drives mutations and
    reads the rows, or seeds rows and reads the endpoint. Here the endpoint answers with
    what the mutations actually wrote, which is the join a screen depends on.
    """
    world = directory.world
    admin = world.acme_admin

    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/gateways/{world.acme_gateway.id}",
            json_body={"system_context": "You are a support assistant."},
        )
    )
    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/models/{world.acme_model.id}",
            json_body={"credential": "sk-rotated"},
        )
    )
    ok(await directory.as_user(admin, "DELETE", f"{API}/keys/{world.acme_key.id}"))
    ok(
        await directory.as_user(
            admin,
            "PATCH",
            f"{API}/members/{world.acme_viewer.id}",
            json_body={"role": "org_member"},
        )
    )

    page = ok(await directory.as_user(admin, "GET", f"{API}/audit-events"))
    entries = page["items"]

    # Newest first, which is the order the screen shows.
    assert [entry["action"] for entry in entries] == [
        "member.update",
        "key.revoke",
        "model.update",
        "gateway.update",
    ]
    for entry in entries:
        assert entry["actor"] == admin.email
        assert entry["actor_type"] == "user"
        assert entry["ip"] is not None
        assert entry["request_id"] is not None

    by_action = {entry["action"]: entry for entry in entries}

    # `before` is a present null rather than an absent one: the gateway had a
    # `system_context` column holding nothing, which is not the same as not having one.
    prompt = by_action["gateway.update"]["changes"]
    assert prompt == [
        {
            "path": "system_context",
            "kind": "changed",
            "before": None,
            "after": "You are a support assistant.",
            "truncated": False,
        }
    ]

    # The one the task names in so many words: that it changed, never what it changed to.
    credential = by_action["model.update"]["changes"]
    assert credential == [
        {
            "path": "credential",
            "kind": "changed",
            "before": "***",
            "after": "***",
            "truncated": False,
        }
    ]

    revoked = by_action["key.revoke"]["changes"]
    assert [change["path"] for change in revoked] == ["revoked_at"]
    assert revoked[0]["before"] is None and revoked[0]["after"] is not None
    assert by_action["key.revoke"]["target"] == world.acme_key.name

    role = by_action["member.update"]["changes"]
    assert role == [
        {
            "path": "role",
            "kind": "changed",
            "before": "org_viewer",
            "after": "org_member",
            "truncated": False,
        }
    ]


async def test_the_export_carries_what_the_screen_shows(directory: DirectoryHarness) -> None:
    """The CSV is the same query with a different renderer, so it has to agree with it."""
    world = directory.world
    ok(
        await directory.as_user(
            world.acme_admin,
            "PATCH",
            f"{API}/models/{world.acme_model.id}",
            json_body={"credential": "sk-exported"},
        )
    )

    response = await directory.as_user(
        world.acme_admin, "GET", f"{API}/audit-events/export?action=model.update"
    )

    assert response.status_code == 200
    assert "model.update" in response.text
    assert "credential: *** -> ***" in response.text
    assert "sk-exported" not in response.text


async def test_a_recording_failure_does_not_fail_the_mutation(
    directory: DirectoryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in a snapshot function loses one event, never somebody's save."""
    import app.services.audit as audit_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("snapshot is broken")

    monkeypatch.setattr(audit_module, "build_event", explode)

    world = directory.world
    ok(
        await directory.as_user(
            world.acme_admin,
            "PATCH",
            f"{API}/gateways/{world.acme_gateway.id}",
            json_body={"name": "Renamed anyway"},
        )
    )
    assert world.database.gateways[world.acme_gateway.id].name == "Renamed anyway"
    assert not _all(world.database.audit_events.values(), "gateway.update")


async def test_one_save_is_one_event_however_many_sections_it_touched(
    directory: DirectoryHarness,
) -> None:
    """The "exactly one" half of the acceptance criterion, which a coverage test alone
    cannot check. The editor writes several sections at once, and three events for one
    press of Save would be three rows nobody can tell apart."""
    world = directory.world

    ok(
        await directory.as_user(
            world.acme_admin,
            "PATCH",
            f"{API}/gateways/{world.acme_gateway.id}",
            json_body={
                "name": "Renamed",
                "system_context": "Be brief.",
                "memory_config": {"doc_top_k": 9},
                "logging_config": {"retention_days": 14},
                "limits": {"requests_per_minute": 60},
            },
        )
    )

    event = _one(world.database.audit_events.values(), "gateway.update")
    paths = {change["path"] for change in event.diff["changes"]}
    assert paths == {
        "name",
        "system_context",
        "memory_config.doc_top_k",
        "logging_config.retention_days",
        "limits.requests_per_minute",
    }


async def test_a_refused_mutation_records_nothing(directory: DirectoryHarness) -> None:
    """The event is in the same transaction, so a rejected save leaves no phantom."""
    world = directory.world

    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"{API}/gateways/{world.acme_gateway.id}",
        json_body={"memory_config": {"doc_top_k": 9999}},
    )
    assert response.status_code == 422

    assert not _all(world.database.audit_events.values(), "gateway.update")


def world_admin(directory: DirectoryHarness) -> Any:
    return directory.world.acme_admin


def _all(events: Any, action: str) -> list[AuditEvent]:
    return sorted((row for row in events if row.action == action), key=lambda row: row.id)


def _one(events: Any, action: str) -> AuditEvent:
    found = _all(events, action)
    assert len(found) == 1, f"expected one {action}, got {len(found)}"
    return found[0]


def test_a_system_attribution_needs_no_user() -> None:
    """The shape a job records with, asserted where a reader will look for it."""
    by = Attribution.system(uuid7(), job="reindex")
    assert by.actor_type == "system" and by.user_id is None


def test_a_scope_with_no_organization_is_the_platforms() -> None:
    assert TenantScope(role="superadmin", organization_id=None).is_platform
    assert not TenantScope(role="org_admin", organization_id=uuid.UUID(int=1)).is_platform
