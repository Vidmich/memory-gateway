"""The behaviour every directory store must have, written once.

The in-memory store exists so the tenancy rules can be tested without a database. That is
only worth anything if it answers like the database does — so the questions the service
asks are asked here, and both implementations run the same checks:
``tests/test_directory_store_memory.py`` and the PostgreSQL half of
``tests/test_directory_db.py``.

The subject is always the same two-organization world, because a single organization
makes every read look correctly scoped: there is nothing else it could have returned.

Not a test module itself (no ``test_`` prefix); it is the shared body those two
parametrize over.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Invitation, Organization, User
from app.services.directory_store import DirectoryStore


@dataclass
class Fixture:
    """Two organizations, two members each, one pending invitation each."""

    store: DirectoryStore
    acme: Organization
    globex: Organization
    acme_admin: User
    acme_viewer: User
    globex_admin: User
    #: A platform account, present so the "belongs to no organization" cases are real.
    superadmin: User
    acme_invitation: Invitation
    globex_invitation: Invitation

    @property
    def acme_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.acme.id)

    @property
    def globex_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.globex.id)

    @property
    def platform_scope(self) -> TenantScope:
        return TenantScope(role="superadmin", organization_id=None)


def make_invitation(
    organization: Organization,
    *,
    email: str,
    role: str = "org_member",
    token_hash: str | None = None,
    expires_in_days: int = 7,
    invited_by: uuid.UUID | None = None,
) -> Invitation:
    return Invitation(
        id=uuid7(),
        organization_id=organization.id,
        email=email,
        role=role,
        token_hash=token_hash or (uuid.uuid4().hex + uuid.uuid4().hex),
        invited_by=invited_by,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
        accepted_at=None,
    )


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


async def an_organization_reads_itself(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.organization(fixture.acme.id)

    assert found is not None and found.id == fixture.acme.id


async def an_organization_cannot_read_another(fixture: Fixture) -> None:
    """The heart of the task: a real id from another tenant is indistinguishable from a
    typo, because both come back as nothing."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.organization(fixture.globex.id) is None
        assert await transaction.organization(uuid7()) is None


async def a_platform_scope_reads_every_organization(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.organization(fixture.acme.id) is not None
        assert await transaction.organization(fixture.globex.id) is not None


async def listing_organizations_is_scoped(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        mine = await transaction.organizations(after=None, limit=50)
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        every = await transaction.organizations(after=None, limit=50)

    assert [organization.id for organization in mine] == [fixture.acme.id]
    assert {fixture.acme.id, fixture.globex.id} <= {row.id for row in every}


async def organizations_are_listed_newest_first(fixture: Fixture) -> None:
    """UUIDv7 ids sort by creation time, which is what makes the cursor a plain id."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        rows = await transaction.organizations(after=None, limit=50)

    ids = [row.id for row in rows]
    assert ids == sorted(ids, reverse=True)


async def a_cursor_skips_what_came_before_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        first_page = await transaction.organizations(after=None, limit=1)
        rest = await transaction.organizations(after=first_page[0].id, limit=50)

    assert all(row.id < first_page[0].id for row in rest)


async def a_slug_is_taken_across_organizations(fixture: Fixture) -> None:
    """Slugs appear in the public gateway URL, so uniqueness is global — and an org
    scope must still see the collision or the insert fails on a constraint it cannot
    explain."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.slug_taken(fixture.globex.slug) is True
        assert await transaction.slug_taken("nobody-has-this") is False


async def a_slug_can_exclude_the_row_being_edited(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.slug_taken(fixture.acme.slug, excluding=fixture.acme.id) is False


async def counts_are_grouped_by_organization(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        counts = await transaction.counts(User, [fixture.acme.id, fixture.globex.id])

    assert counts.get(fixture.acme.id) == 2
    assert counts.get(fixture.globex.id) == 1
    # The platform account belongs to neither, so it is in no bucket.
    assert sum(counts.values()) == 3


async def counting_nothing_asks_nothing(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.counts(User, []) == {}


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


async def a_member_is_found_in_scope(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.member(fixture.acme_viewer.id)

    assert found is not None and found.id == fixture.acme_viewer.id


async def a_member_of_another_organization_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.member(fixture.globex_admin.id) is None


async def listing_members_is_scoped(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.members(after=None, limit=50)

    assert {row.id for row in rows} == {fixture.acme_admin.id, fixture.acme_viewer.id}


async def a_platform_account_is_nobodys_member(fixture: Fixture) -> None:
    """A superadmin has ``organization_id IS NULL``, so the scope clause excludes them
    from every organization's member list without a special case."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.members(after=None, limit=50)

        assert fixture.superadmin.id not in {row.id for row in rows}
        assert await transaction.member(fixture.superadmin.id) is None
        assert all(row.organization_id == fixture.acme.id for row in rows)


async def active_admins_are_scoped(fixture: Fixture) -> None:
    """The last-admin guard reads this. If it counted across organizations, Acme's last
    admin could be removed because Globex has one."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        admins = await transaction.active_admins()

    assert [admin.id for admin in admins] == [fixture.acme_admin.id]


async def a_suspended_admin_does_not_count(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        admin = await transaction.member(fixture.acme_admin.id)
        assert admin is not None
        admin.status = "suspended"
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.active_admins() == []


async def an_email_is_taken_across_organizations(fixture: Fixture) -> None:
    """``users.email`` is globally unique, so the check has to be global — otherwise the
    insert fails on a constraint the admin cannot see."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.email_taken(fixture.globex_admin.email) is True
        assert await transaction.email_taken("nobody@example.com") is False


async def the_email_check_ignores_case_and_whitespace(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.email_taken(f"  {fixture.acme_viewer.email.upper()} ") is True


async def adding_a_member_stamps_the_scope(fixture: Fixture) -> None:
    """The caller does not choose the organization: whatever they set is overwritten."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        user = User(
            id=uuid7(),
            organization_id=fixture.globex.id,  # ignored
            email="fresh@acme.test",
            role="org_member",
            name="Fresh",
            status="active",
        )
        await transaction.add_user(user)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.member(user.id)

    assert found is not None and found.organization_id == fixture.acme.id


async def removing_a_member_removes_them(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        member = await transaction.member(fixture.acme_viewer.id)
        assert member is not None
        await transaction.delete_user(member)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.member(fixture.acme_viewer.id) is None


async def mutating_a_returned_member_persists(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        member = await transaction.member(fixture.acme_viewer.id)
        assert member is not None
        member.role = "org_member"
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        again = await transaction.member(fixture.acme_viewer.id)

    assert again is not None and again.role == "org_member"


# ---------------------------------------------------------------------------
# invitations
# ---------------------------------------------------------------------------


async def an_invitation_is_found_in_scope(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.invitation(fixture.acme_invitation.id)

    assert found is not None and found.id == fixture.acme_invitation.id


async def an_invitation_of_another_organization_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.invitation(fixture.globex_invitation.id) is None


async def listing_invitations_is_scoped(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.invitations(after=None, limit=50)

    assert [row.id for row in rows] == [fixture.acme_invitation.id]


async def a_token_hash_resolves_regardless_of_scope(fixture: Fixture) -> None:
    """Acceptance is unauthenticated, so there is no scope to resolve it in: the token is
    the claim, and the row it finds is what establishes the tenant."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.invitation_by_token_hash(fixture.globex_invitation.token_hash)

    assert found is not None and found.id == fixture.globex_invitation.id


async def an_unknown_token_hash_resolves_to_nothing(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.invitation_by_token_hash("0" * 64) is None


async def a_pending_invitation_is_found_by_address(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.pending_invitation_for(fixture.acme_invitation.email)

    assert found is not None and found.id == fixture.acme_invitation.id


async def another_organizations_pending_invitation_is_not(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.pending_invitation_for(fixture.globex_invitation.email) is None


async def two_organizations_can_invite_the_same_person(fixture: Fixture) -> None:
    """And neither learns about the other. The uniqueness index is partial and per
    organization, precisely so a contractor can belong to two customers."""
    shared = fixture.acme_invitation.email
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        await transaction.add_invitation(make_invitation(fixture.globex, email=shared))
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.pending_invitation_for(shared)

    assert found is not None and found.id == fixture.acme_invitation.id


async def an_accepted_invitation_is_no_longer_pending(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        invitation = await transaction.invitation(fixture.acme_invitation.id)
        assert invitation is not None
        invitation.accepted_at = datetime.now(UTC)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.pending_invitation_for(fixture.acme_invitation.email) is None


async def adding_an_invitation_stamps_the_scope(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        invitation = make_invitation(fixture.globex, email="new@acme.test")  # org ignored
        await transaction.add_invitation(invitation)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.invitation(invitation.id)

    assert found is not None and found.organization_id == fixture.acme.id


async def revoking_an_invitation_removes_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        invitation = await transaction.invitation(fixture.acme_invitation.id)
        assert invitation is not None
        await transaction.delete_invitation(invitation)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.invitation(fixture.acme_invitation.id) is None


# ---------------------------------------------------------------------------
# narrowing
# ---------------------------------------------------------------------------


async def narrowing_restricts_a_platform_scope(fixture: Fixture) -> None:
    """How a superadmin acts inside one organization: same unit of work, tighter scope."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        inner = transaction.narrowed(fixture.globex_scope)

        assert await inner.member(fixture.globex_admin.id) is not None
        assert await inner.member(fixture.acme_admin.id) is None
        assert [admin.id for admin in await inner.active_admins()] == [fixture.globex_admin.id]


async def narrowing_shares_the_unit_of_work(fixture: Fixture) -> None:
    """A write through the narrowed view has to be visible to the outer one, or a service
    that reads back after writing would see stale rows."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        inner = transaction.narrowed(fixture.acme_scope)
        user = User(
            id=uuid7(),
            email="narrowed@acme.test",
            role="org_viewer",
            name="Narrowed",
            status="active",
        )
        await inner.add_user(user)

        assert await transaction.member(user.id) is not None
        await transaction.commit()


Check = Callable[[Fixture], Awaitable[None]]

#: Every check, in one list, so neither implementation can be given a shorter exam.
CHECKS: tuple[Check, ...] = (
    an_organization_reads_itself,
    an_organization_cannot_read_another,
    a_platform_scope_reads_every_organization,
    listing_organizations_is_scoped,
    organizations_are_listed_newest_first,
    a_cursor_skips_what_came_before_it,
    a_slug_is_taken_across_organizations,
    a_slug_can_exclude_the_row_being_edited,
    counts_are_grouped_by_organization,
    counting_nothing_asks_nothing,
    a_member_is_found_in_scope,
    a_member_of_another_organization_is_not_found,
    listing_members_is_scoped,
    a_platform_account_is_nobodys_member,
    active_admins_are_scoped,
    a_suspended_admin_does_not_count,
    an_email_is_taken_across_organizations,
    the_email_check_ignores_case_and_whitespace,
    adding_a_member_stamps_the_scope,
    removing_a_member_removes_them,
    mutating_a_returned_member_persists,
    an_invitation_is_found_in_scope,
    an_invitation_of_another_organization_is_not_found,
    listing_invitations_is_scoped,
    a_token_hash_resolves_regardless_of_scope,
    an_unknown_token_hash_resolves_to_nothing,
    a_pending_invitation_is_found_by_address,
    another_organizations_pending_invitation_is_not,
    two_organizations_can_invite_the_same_person,
    an_accepted_invitation_is_no_longer_pending,
    adding_an_invitation_stamps_the_scope,
    revoking_an_invitation_removes_it,
    narrowing_restricts_a_platform_scope,
    narrowing_shares_the_unit_of_work,
)
