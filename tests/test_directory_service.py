"""The rules: invitation lifecycle, the last-admin guard, and organization editing.

These run against the in-memory store, over the two-organization world in
``tests/directory_support.py``. What they are checking is state machines and guards, not
SQL — the SQL is checked by ``tests/test_scoping.py`` and by the store contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import Conflict, Forbidden, NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.services.directory import INVITATION_UNUSABLE, Actor, DirectoryService
from tests.directory_support import World, build_world

PASSWORD = "a-perfectly-fine-password"


@pytest.fixture
def world() -> World:
    return build_world()


def service(world: World) -> DirectoryService:
    return world.directory


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


async def test_a_superadmin_sees_every_organization(world: World) -> None:
    page = await service(world).list_organizations(world.actor(world.superadmin))

    assert {view.organization.slug for view in page.items} == {"acme", "globex"}


async def test_an_org_user_sees_only_their_own(world: World) -> None:
    page = await service(world).list_organizations(world.actor(world.acme_viewer))

    assert [view.organization.slug for view in page.items] == ["acme"]


async def test_the_list_carries_member_counts(world: World) -> None:
    page = await service(world).list_organizations(world.actor(world.superadmin))
    counts = {view.organization.slug: view.member_count for view in page.items}

    assert counts == {"acme": 3, "globex": 1}


async def test_reading_another_organization_is_a_404(world: World) -> None:
    with pytest.raises(NotFound):
        await service(world).get_organization(world.actor(world.acme_admin), world.globex.id)


async def test_a_missing_organization_answers_the_same_way(world: World) -> None:
    """Identical to the cross-tenant case, on purpose: the two must not be tellable
    apart, or the id becomes an existence oracle."""
    with pytest.raises(NotFound):
        await service(world).get_organization(world.actor(world.acme_admin), uuid7())


async def test_creating_an_organization(world: World) -> None:
    created = await service(world).create_organization(
        world.actor(world.superadmin), name="Initech", slug="initech"
    )

    assert created.slug == "initech"
    assert created.status == "active"
    assert created.settings == {}


async def test_a_duplicate_slug_is_a_conflict(world: World) -> None:
    with pytest.raises(Conflict):
        await service(world).create_organization(
            world.actor(world.superadmin), name="Another Acme", slug="acme"
        )


async def test_an_admin_can_rename_their_own_organization(world: World) -> None:
    updated = await service(world).update_organization(
        world.actor(world.acme_admin), world.acme.id, name="Acme Corporation"
    )

    assert updated.name == "Acme Corporation"
    assert updated.slug == "acme"  # untouched fields stay untouched


async def test_updating_another_organization_is_a_404(world: World) -> None:
    with pytest.raises(NotFound):
        await service(world).update_organization(
            world.actor(world.acme_admin), world.globex.id, name="Owned"
        )


async def test_settings_round_trip(world: World) -> None:
    updated = await service(world).update_organization(
        world.actor(world.acme_admin), world.acme.id, settings={"distillation_model": "x"}
    )

    assert updated.settings == {"distillation_model": "x"}


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


async def test_listing_members_is_scoped(world: World) -> None:
    page = await service(world).list_members(world.actor(world.acme_admin), world.acme.id)

    assert {member.email for member in page.items} == {
        world.acme_admin.email,
        world.acme_member.email,
        world.acme_viewer.email,
    }


async def test_listing_another_organizations_members_is_a_404(world: World) -> None:
    with pytest.raises(NotFound):
        await service(world).list_members(world.actor(world.acme_admin), world.globex.id)


async def test_a_role_can_be_changed(world: World) -> None:
    updated = await service(world).update_member(
        world.actor(world.acme_admin), world.acme_viewer.id, role="org_member"
    )

    assert updated.role == "org_member"


async def test_a_member_of_another_organization_is_a_404(world: World) -> None:
    with pytest.raises(NotFound):
        await service(world).update_member(
            world.actor(world.acme_admin), world.globex_admin.id, role="org_viewer"
        )


async def test_a_platform_account_is_not_a_member(world: World) -> None:
    """Even for a superadmin. Reaching another platform account through the members API
    would be a way to demote or delete one with no screen and no guard."""
    with pytest.raises(NotFound):
        await service(world).update_member(
            world.actor(world.superadmin), world.superadmin.id, role="org_viewer"
        )


async def test_a_member_can_be_removed(world: World) -> None:
    await service(world).remove_member(world.actor(world.acme_admin), world.acme_viewer.id)

    page = await service(world).list_members(world.actor(world.acme_admin), world.acme.id)
    assert world.acme_viewer.id not in {member.id for member in page.items}


# -- the last admin --------------------------------------------------------


async def test_the_last_admin_cannot_be_demoted(world: World) -> None:
    with pytest.raises(Conflict, match="only active administrator"):
        await service(world).update_member(
            world.actor(world.acme_admin), world.acme_admin.id, role="org_member"
        )


async def test_the_last_admin_cannot_be_suspended(world: World) -> None:
    with pytest.raises(Conflict):
        await service(world).update_member(
            world.actor(world.acme_admin), world.acme_admin.id, status="suspended"
        )


async def test_the_last_admin_cannot_be_removed(world: World) -> None:
    with pytest.raises(Conflict):
        await service(world).remove_member(world.actor(world.acme_admin), world.acme_admin.id)


async def test_a_superadmin_cannot_remove_the_last_admin_either(world: World) -> None:
    """The guard protects the organization, not the actor. A support engineer emptying
    the admin slot leaves the customer unable to invite anyone."""
    with pytest.raises(Conflict):
        await service(world).remove_member(world.actor(world.superadmin), world.acme_admin.id)


async def test_a_second_admin_unlocks_the_first(world: World) -> None:
    await service(world).update_member(
        world.actor(world.acme_admin), world.acme_member.id, role="org_admin"
    )
    await service(world).update_member(
        world.actor(world.acme_admin), world.acme_admin.id, role="org_member"
    )

    assert world.acme_admin.role == "org_member"


async def test_a_suspended_admin_does_not_count_as_the_second(world: World) -> None:
    await service(world).update_member(
        world.actor(world.acme_admin), world.acme_member.id, role="org_admin"
    )
    await service(world).update_member(
        world.actor(world.acme_admin), world.acme_member.id, status="suspended"
    )

    with pytest.raises(Conflict):
        await service(world).remove_member(world.actor(world.acme_admin), world.acme_admin.id)


async def test_the_last_admin_of_one_organization_is_not_saved_by_another(
    world: World,
) -> None:
    """Globex has an admin. That must not make Acme's removable."""
    with pytest.raises(Conflict):
        await service(world).remove_member(world.actor(world.acme_admin), world.acme_admin.id)


# ---------------------------------------------------------------------------
# invitations
# ---------------------------------------------------------------------------


async def invite(world: World, *, email: str = "new@acme.example.com", role: str = "org_member"):  # type: ignore[no-untyped-def]
    return await service(world).invite(
        world.actor(world.acme_admin), world.acme.id, email=email, role=role
    )


async def test_an_invitation_yields_a_link_exactly_once(world: World) -> None:
    issued = await invite(world)

    assert issued.token
    assert issued.url("https://app.example.com").endswith(issued.token)
    # Nothing stores the plaintext; the row carries only its hash.
    assert issued.token not in issued.invitation.token_hash


async def test_inviting_into_another_organization_is_a_404(world: World) -> None:
    with pytest.raises(NotFound):
        await service(world).invite(
            world.actor(world.acme_admin), world.globex.id, email="x@y.test", role="org_member"
        )


async def test_a_superadmin_cannot_be_invited(world: World) -> None:
    with pytest.raises(Validation):
        await invite(world, role="superadmin")


async def test_inviting_an_existing_account_is_a_conflict(world: World) -> None:
    with pytest.raises(Conflict):
        await invite(world, email=world.acme_member.email)


async def test_inviting_someone_who_belongs_elsewhere_says_the_same_thing(
    world: World,
) -> None:
    """ "Already has an account" is all an admin needs; naming the other organization
    would disclose a membership they have no business knowing about."""
    with pytest.raises(Conflict, match="already has an account"):
        await invite(world, email=world.globex_admin.email)


async def test_a_second_pending_invitation_is_refused(world: World) -> None:
    await invite(world)
    with pytest.raises(Conflict, match="pending invitation"):
        await invite(world)


async def test_an_invitation_can_be_previewed(world: World) -> None:
    issued = await invite(world, role="org_viewer")
    preview = await service(world).preview_invitation(issued.token)

    assert preview.email == "new@acme.example.com"
    assert preview.role == "org_viewer"
    assert preview.organization_name == "Acme"


async def test_a_wrong_token_is_a_404(world: World) -> None:
    with pytest.raises(NotFound, match=INVITATION_UNUSABLE):
        await service(world).preview_invitation("not-a-token")


async def test_accepting_creates_an_active_member(world: World) -> None:
    issued = await invite(world, role="org_viewer")

    user = await service(world).accept_invitation(
        issued.token, name="New Person", password=PASSWORD
    )

    assert user.email == "new@acme.example.com"
    assert user.role == "org_viewer"
    assert user.status == "active"
    assert user.organization_id == world.acme.id
    assert user.password_hash is not None


async def test_an_invitation_is_single_use(world: World) -> None:
    issued = await invite(world)
    await service(world).accept_invitation(issued.token, name="First", password=PASSWORD)

    with pytest.raises(NotFound, match=INVITATION_UNUSABLE):
        await service(world).accept_invitation(issued.token, name="Second", password=PASSWORD)


async def test_an_accepted_invitation_cannot_be_previewed(world: World) -> None:
    issued = await invite(world)
    await service(world).accept_invitation(issued.token, name="First", password=PASSWORD)

    with pytest.raises(NotFound):
        await service(world).preview_invitation(issued.token)


async def test_an_expired_invitation_is_refused(world: World) -> None:
    issued = await invite(world)
    issued.invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(NotFound, match=INVITATION_UNUSABLE):
        await service(world).accept_invitation(issued.token, name="Late", password=PASSWORD)


async def test_a_weak_password_is_refused_before_anything_is_burned(world: World) -> None:
    issued = await invite(world)

    with pytest.raises(Validation):
        await service(world).accept_invitation(issued.token, name="New", password="short")

    # Still usable: a rejected password must not consume the invitation.
    assert (await service(world).preview_invitation(issued.token)).email == "new@acme.example.com"


async def test_accepting_into_a_suspended_organization_is_refused(world: World) -> None:
    issued = await invite(world)
    await service(world).update_organization(
        world.actor(world.superadmin), world.acme.id, status="suspended"
    )

    with pytest.raises(NotFound):
        await service(world).accept_invitation(issued.token, name="New", password=PASSWORD)


async def test_revoking_an_invitation(world: World) -> None:
    issued = await invite(world)
    await service(world).revoke_invitation(world.actor(world.acme_admin), issued.invitation.id)

    with pytest.raises(NotFound):
        await service(world).preview_invitation(issued.token)


async def test_revoking_another_organizations_invitation_is_a_404(world: World) -> None:
    issued = await service(world).invite(
        world.actor(world.globex_admin),
        world.globex.id,
        email="g@globex.example.com",
        role="org_member",
    )

    with pytest.raises(NotFound):
        await service(world).revoke_invitation(world.actor(world.acme_admin), issued.invitation.id)


async def test_resending_rotates_the_link(world: World) -> None:
    """The old link stops working. That is the safer reading of "resend": if the first
    one went to the wrong address, this takes it away."""
    first = await invite(world)
    second = await service(world).resend_invitation(
        world.actor(world.acme_admin), first.invitation.id
    )

    assert second.token != first.token
    assert (await service(world).preview_invitation(second.token)).email == "new@acme.example.com"
    with pytest.raises(NotFound):
        await service(world).preview_invitation(first.token)


async def test_resending_extends_the_expiry(world: World) -> None:
    first = await invite(world)
    first.invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    second = await service(world).resend_invitation(
        world.actor(world.acme_admin), first.invitation.id
    )

    assert second.invitation.expires_at > datetime.now(UTC)


async def test_an_accepted_invitation_cannot_be_resent(world: World) -> None:
    issued = await invite(world)
    await service(world).accept_invitation(issued.token, name="New", password=PASSWORD)

    with pytest.raises(Conflict):
        await service(world).resend_invitation(world.actor(world.acme_admin), issued.invitation.id)


async def test_listing_invitations_is_scoped(world: World) -> None:
    await invite(world)
    await service(world).invite(
        world.actor(world.globex_admin),
        world.globex.id,
        email="g@globex.example.com",
        role="org_member",
    )

    page = await service(world).list_invitations(world.actor(world.acme_admin))

    assert [invitation.email for invitation in page.items] == ["new@acme.example.com"]


# ---------------------------------------------------------------------------
# superadmin narrowing
# ---------------------------------------------------------------------------


async def test_a_superadmin_acts_inside_an_organization(world: World) -> None:
    issued = await service(world).invite(
        world.actor(world.superadmin),
        world.acme.id,
        email="support@acme.example.com",
        role="org_member",
    )

    assert issued.invitation.organization_id == world.acme.id


async def test_narrowing_is_recorded(world: World, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.core.tenancy"):
        await service(world).list_members(world.actor(world.superadmin), world.globex.id)

    assert any(r.message == "superadmin assumed organization" for r in caplog.records)


async def test_an_org_admin_narrowing_to_itself_is_not_recorded(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """Only cross-org access is an event. Logging an admin reading their own members
    would bury the accesses that matter."""
    with caplog.at_level("INFO", logger="app.core.tenancy"):
        await service(world).list_members(world.actor(world.acme_admin), world.acme.id)

    assert not any(r.message == "superadmin assumed organization" for r in caplog.records)


async def test_a_platform_scope_cannot_invite_without_an_organization(world: World) -> None:
    """`require_organization` is the backstop: a write with no tenant has nowhere to go."""
    actor = Actor(
        user_id=world.superadmin.id,
        scope=TenantScope(role="superadmin", organization_id=None),
    )
    with pytest.raises((Forbidden, NotFound)):
        await service(world).invite(actor, uuid7(), email="nowhere@example.com", role="org_member")
