"""Organizations, members and invitations — the rules, with the storage behind a port.

Three things in here are load-bearing and easy to get subtly wrong.

**Cross-tenant access is a 404, not a 403.** A 403 confirms the id exists, which turns
any list endpoint into an oracle for enumerating another organization's resources. Since
the scope is applied in the query, an out-of-scope row simply is not found, and the
"missing" and "not yours" cases become genuinely indistinguishable rather than
deliberately conflated at the last moment.

**A superadmin narrowing to one organization is an event.** SPEC §5.2 requires every
support access to be recorded, so widening happens only through
:meth:`TenantScope.assume`, which logs. There is no other path from platform scope to a
single organization's rows.

**An organization must keep an admin.** Otherwise its members lose the ability to invite
one, and recovering needs a platform administrator and a support ticket. The check counts
*active* admins, and it runs for demotion, suspension and removal alike — including when
the actor is doing it to themselves, which is how it usually happens.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import tokens
from app.core.config import Settings, get_settings
from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.core.passwords import Hasher, PasswordPolicy
from app.core.tenancy import Actor, TenantScope
from app.db.models import Gateway, Invitation, Organization, User
from app.db.models.invitation import INVITABLE_ROLES
from app.schemas.gateway_config import (
    LoggingConfig,
    TemplateConfig,
    merge_config,
    organization_logging_defaults,
    organization_template_defaults,
)
from app.services.audit import Attribution
from app.services.audit_snapshots import subject
from app.services.directory_store import DirectoryStore, DirectoryTransaction
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of

logger = logging.getLogger(__name__)

#: How long an invitation link works for. SPEC does not fix this; seven days is long
#: enough to survive a holiday and short enough that a forwarded email goes stale.
INVITATION_TTL_DAYS = 7

#: Deliberately vague, and identical for "no such invitation", "already accepted" and
#: "expired". The token is a bearer credential handed out by email, so a caller holding a
#: wrong one learns nothing about which of those it is.
INVITATION_UNUSABLE = "This invitation link is no longer valid. Ask for a new one."


@dataclass(frozen=True, slots=True)
class OrganizationView:
    """An organization plus the counts the list screen shows.

    Counts are gathered for a whole page in one query rather than per row: the platform
    list is the screen most likely to grow to hundreds of organizations.
    """

    organization: Organization
    member_count: int
    gateway_count: int


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """The invitation row plus the token, which exists only here.

    Only ``sha256(token)`` is stored, so this is the one moment the link can be shown.
    The API returns it on create and on resend, and never again.
    """

    invitation: Invitation
    organization: Organization
    token: str

    def url(self, base: str) -> str:
        return f"{base.rstrip('/')}/invitations/accept/{self.token}"


@dataclass(frozen=True, slots=True)
class InvitationPreview:
    """What the acceptance page may show before anyone has authenticated."""

    email: str
    role: str
    organization_name: str
    expires_at: datetime


class DirectoryService:
    def __init__(
        self,
        store: DirectoryStore,
        *,
        hasher: Hasher,
        settings: Settings | None = None,
        policy: PasswordPolicy | None = None,
    ) -> None:
        self._store = store
        self._hasher = hasher
        self._settings = settings or get_settings()
        self._policy = policy or PasswordPolicy()

    # -- organizations ----------------------------------------------------

    async def list_organizations(
        self, actor: Actor, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[OrganizationView]:
        """Every organization for a platform admin; exactly one for everyone else.

        An org user is not refused here — they get a single-item list, which is what the
        UI needs to render "your organization" without a second endpoint shape.
        """
        size = clamp_limit(limit)
        after = decode_cursor(cursor)

        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.organizations(after=after, limit=size)
            page = page_of(rows, limit=size, cursor_of=lambda row: row.id)
            ids = [organization.id for organization in page.items]
            members = await transaction.counts(User, ids)
            gateways = await transaction.counts(Gateway, ids)

        return Page(
            items=tuple(
                OrganizationView(
                    organization=organization,
                    member_count=members.get(organization.id, 0),
                    gateway_count=gateways.get(organization.id, 0),
                )
                for organization in page.items
            ),
            next_cursor=page.next_cursor,
        )

    async def get_organization(self, actor: Actor, organization_id: uuid.UUID) -> Organization:
        async with self._store.begin(actor.scope) as transaction:
            return await self._organization_or_404(transaction, organization_id)

    async def create_organization(self, actor: Actor, *, name: str, slug: str) -> Organization:
        async with self._store.begin(actor.scope) as transaction:
            if await transaction.slug_taken(slug):
                raise Conflict(f"The slug '{slug}' is already in use.", param="slug")

            organization = Organization(
                id=uuid7(), name=name.strip(), slug=slug, status="active", settings={}
            )
            await transaction.add_organization(organization)
            # The new organization's own log, not the platform's: the first thing that
            # ever happened to a customer is that somebody created them.
            transaction.audit(
                actor,
                "organization.create",
                after=subject(organization),
                organization_id=organization.id,
            )
            await transaction.commit()

        logger.info(
            "organization created",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization.id),
                "audit_action": "organization.create",
            },
        )
        return organization

    async def update_organization(
        self,
        actor: Actor,
        organization_id: uuid.UUID,
        *,
        name: str | None = None,
        slug: str | None = None,
        status: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Organization:
        """Partial update. ``status`` is platform-only — an organization must not be able
        to suspend or un-suspend itself — and the route enforces that before calling."""
        async with self._store.begin(actor.scope) as transaction:
            organization = await self._organization_or_404(transaction, organization_id)
            before = subject(organization)

            if slug is not None and slug != organization.slug:
                if await transaction.slug_taken(slug, excluding=organization.id):
                    raise Conflict(f"The slug '{slug}' is already in use.", param="slug")
                organization.slug = slug
            if name is not None:
                organization.name = name.strip()
            if status is not None:
                organization.status = status
            if settings is not None:
                _check_logging_defaults(settings)
                _check_template_defaults(settings)
                organization.settings = settings

            transaction.audit(
                actor,
                "organization.update",
                before=before,
                after=subject(organization),
                organization_id=organization.id,
            )
            await transaction.commit()

        logger.info(
            "organization updated",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization.id),
                "audit_action": "organization.update",
            },
        )
        return organization

    # -- members ----------------------------------------------------------

    async def list_members(
        self,
        actor: Actor,
        organization_id: uuid.UUID,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[User]:
        size = clamp_limit(limit)
        after = decode_cursor(cursor)

        async with self._store.begin(actor.scope) as transaction:
            inner = await self._narrow(transaction, actor, organization_id)
            rows = await inner.members(after=after, limit=size)

        return page_of(rows, limit=size, cursor_of=lambda row: row.id)

    async def update_member(
        self,
        actor: Actor,
        member_id: uuid.UUID,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> User:
        async with self._store.begin(actor.scope) as transaction:
            member = await transaction.member(member_id)
            if member is None or member.organization_id is None:
                # A platform account belongs to no organization, so it is not a member of
                # one and is not managed here. Without this, a superadmin at platform
                # scope could reach another superadmin through the members API.
                raise NotFound("No such member.")

            inner = await self._narrow(transaction, actor, member.organization_id)
            before = subject(member)

            losing_admin = (role is not None and role != "org_admin") or (
                status is not None and status != "active"
            )
            if member.role == "org_admin" and member.status == "active" and losing_admin:
                await self._require_another_admin(inner, member)

            if role is not None:
                if role not in INVITABLE_ROLES:
                    raise Validation(
                        f"A member's role must be one of {', '.join(INVITABLE_ROLES)}.",
                        param="role",
                    )
                member.role = role
            if status is not None:
                member.status = status

            # The organization is named explicitly because a platform administrator's
            # own scope has none. That is also what `Attribution.inside` reads to mark the
            # event as support access, so this call site does not have to know it is one.
            transaction.audit(
                actor,
                "member.update",
                before=before,
                after=subject(member),
                organization_id=member.organization_id,
            )
            await transaction.commit()

        logger.info(
            "member updated",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(member.organization_id),
                "target_user_id": str(member.id),
                "audit_action": "member.update",
            },
        )
        return member

    async def remove_member(self, actor: Actor, member_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            member = await transaction.member(member_id)
            if member is None or member.organization_id is None:
                # A platform account belongs to no organization, so it is not a member of
                # one and is not managed here. Without this, a superadmin at platform
                # scope could reach another superadmin through the members API.
                raise NotFound("No such member.")

            inner = await self._narrow(transaction, actor, member.organization_id)
            if member.role == "org_admin" and member.status == "active":
                await self._require_another_admin(inner, member)

            organization_id = member.organization_id
            transaction.audit(
                actor,
                "member.remove",
                before=subject(member),
                organization_id=organization_id,
            )
            await inner.delete_user(member)
            await transaction.commit()

        logger.info(
            "member removed",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "target_user_id": str(member_id),
                "audit_action": "member.remove",
            },
        )

    # -- invitations ------------------------------------------------------

    async def invite(
        self,
        actor: Actor,
        organization_id: uuid.UUID,
        *,
        email: str,
        role: str,
    ) -> IssuedInvitation:
        if role not in INVITABLE_ROLES:
            raise Validation(
                f"An invitation's role must be one of {', '.join(INVITABLE_ROLES)}.",
                param="role",
            )

        address = email.strip()
        async with self._store.begin(actor.scope) as transaction:
            organization = await self._organization_or_404(transaction, organization_id)
            inner = await self._narrow(transaction, actor, organization_id)

            if await inner.email_taken(address):
                # Deliberately the same answer whether the account is in this
                # organization or another: "already has an account" is all an admin needs,
                # and more would disclose membership elsewhere.
                raise Conflict("That email address already has an account.", param="email")
            if await inner.pending_invitation_for(address) is not None:
                raise Conflict(
                    "That address already has a pending invitation. "
                    "Revoke it, or send a new link from the existing one.",
                    param="email",
                )

            minted = tokens.mint_opaque_token()
            invitation = Invitation(
                id=uuid7(),
                organization_id=organization_id,
                email=address,
                role=role,
                token_hash=minted.token_hash,
                invited_by=actor.user_id,
                expires_at=datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS),
                accepted_at=None,
            )
            await inner.add_invitation(invitation)
            transaction.audit(
                actor,
                "invitation.create",
                after=subject(invitation),
                organization_id=organization_id,
            )
            await transaction.commit()

        logger.info(
            "invitation created",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "audit_action": "invitation.create",
            },
        )
        return IssuedInvitation(
            invitation=invitation, organization=organization, token=minted.token
        )

    async def list_invitations(
        self, actor: Actor, *, cursor: str | None = None, limit: int | None = None
    ) -> Page[Invitation]:
        size = clamp_limit(limit)
        after = decode_cursor(cursor)

        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.invitations(after=after, limit=size)

        return page_of(rows, limit=size, cursor_of=lambda row: row.id)

    async def revoke_invitation(self, actor: Actor, invitation_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            invitation = await transaction.invitation(invitation_id)
            if invitation is None:
                raise NotFound("No such invitation.")

            inner = await self._narrow(transaction, actor, invitation.organization_id)
            organization_id = invitation.organization_id
            transaction.audit(
                actor,
                "invitation.revoke",
                before=subject(invitation),
                organization_id=organization_id,
            )
            await inner.delete_invitation(invitation)
            await transaction.commit()

        logger.info(
            "invitation revoked",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "audit_action": "invitation.revoke",
            },
        )

    async def resend_invitation(self, actor: Actor, invitation_id: uuid.UUID) -> IssuedInvitation:
        """Mint a fresh token for an existing invitation, invalidating the old link.

        The stored value is a hash, so the original link is genuinely unrecoverable —
        which is the point. Rotating is also the safer reading of "resend": if the first
        link went to the wrong address, this takes it away.
        """
        async with self._store.begin(actor.scope) as transaction:
            invitation = await transaction.invitation(invitation_id)
            if invitation is None:
                raise NotFound("No such invitation.")
            if invitation.is_accepted:
                raise Conflict("That invitation has already been accepted.")

            organization = await self._organization_or_404(transaction, invitation.organization_id)
            await self._narrow(transaction, actor, invitation.organization_id)
            before = subject(invitation)

            minted = tokens.mint_opaque_token()
            invitation.token_hash = minted.token_hash
            invitation.expires_at = datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS)
            # The diff says the token changed and the expiry moved, and says nothing
            # about what the token became — it is a bearer credential, so it is marked
            # sensitive in the snapshot and compared by fingerprint.
            transaction.audit(
                actor,
                "invitation.resend",
                before=before,
                after=subject(invitation),
                organization_id=invitation.organization_id,
            )
            await transaction.commit()

        logger.info(
            "invitation link rotated",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(invitation.organization_id),
                "audit_action": "invitation.resend",
            },
        )
        return IssuedInvitation(
            invitation=invitation, organization=organization, token=minted.token
        )

    # -- acceptance (unauthenticated) -------------------------------------

    async def preview_invitation(self, token: str) -> InvitationPreview:
        """Validate a link before showing the form.

        Runs at platform scope because the caller has no session yet — the token is the
        claim, and the row it resolves to is what establishes the organization.
        """
        async with self._store.begin(_ACCEPTANCE_SCOPE) as transaction:
            invitation = await self._usable_invitation(transaction, token)
            organization = await transaction.organization(invitation.organization_id)
            if organization is None or not organization.is_active:
                raise NotFound(INVITATION_UNUSABLE)

            return InvitationPreview(
                email=invitation.email,
                role=invitation.role,
                organization_name=organization.name,
                expires_at=invitation.expires_at,
            )

    async def accept_invitation(self, token: str, *, name: str, password: str) -> User:
        """Create the member, and burn the invitation.

        Single-use is enforced by stamping ``accepted_at`` in the same transaction that
        creates the user, so a replayed link finds an already-accepted row. The globally
        unique email index is the backstop if two requests race.
        """
        reason = self._policy.check(password)
        if reason is not None:
            raise Validation(reason, param="password")

        async with self._store.begin(_ACCEPTANCE_SCOPE) as transaction:
            invitation = await self._usable_invitation(transaction, token)
            organization = await transaction.organization(invitation.organization_id)
            if organization is None or not organization.is_active:
                raise NotFound(INVITATION_UNUSABLE)

            inner = transaction.narrowed(
                TenantScope(role=invitation.role, organization_id=organization.id)
            )
            if await inner.email_taken(invitation.email):
                raise Conflict("That email address already has an account.", param="email")

            user = User(
                id=uuid7(),
                organization_id=organization.id,
                email=invitation.email,
                password_hash=self._hasher.hash(password),
                role=invitation.role,
                name=name.strip() or invitation.email,
                status="active",
                last_login_at=None,
            )
            await inner.add_user(user)
            invitation.accepted_at = datetime.now(UTC)
            # The actor is the person who just accepted: there is no session yet, so the
            # attribution is built by hand rather than from an `Actor`. It is a `user`
            # event, not a platform one — the organization is theirs from this moment.
            inner.audit(
                Attribution(
                    actor_type="user",
                    user_id=user.id,
                    label=user.email,
                    organization_id=organization.id,
                ),
                "invitation.accept",
                after=subject(user),
            )
            await transaction.commit()

        logger.info(
            "invitation accepted",
            extra={
                "user_id": str(user.id),
                "organization_id": str(organization.id),
                "audit_action": "invitation.accept",
            },
        )
        return user

    # -- internals --------------------------------------------------------

    async def _organization_or_404(
        self, transaction: DirectoryTransaction, organization_id: uuid.UUID
    ) -> Organization:
        organization = await transaction.organization(organization_id)
        if organization is None:
            # Also the answer when it exists and belongs to someone else. That is the
            # point: an id from another tenant is indistinguishable from a typo.
            raise NotFound("No such organization.")
        return organization

    async def _narrow(
        self, transaction: DirectoryTransaction, actor: Actor, organization_id: uuid.UUID
    ) -> DirectoryTransaction:
        """A view of this unit of work scoped to one organization.

        For an org user it is their own scope, unchanged, and an id belonging to anyone
        else has already failed :meth:`TenantScope.permits`. For a superadmin it is an
        assumed scope, which is logged.
        """
        scope = actor.scope
        if not scope.permits(organization_id):
            raise NotFound("No such organization.")
        if scope.is_platform:
            return transaction.narrowed(scope.assume(organization_id, actor_user_id=actor.user_id))
        return transaction

    async def _require_another_admin(self, transaction: DirectoryTransaction, member: User) -> None:
        admins = await transaction.active_admins()
        if not any(candidate.id != member.id for candidate in admins):
            raise Conflict(
                "This is the organization's only active administrator. Appoint another one first."
            )

    async def _usable_invitation(self, transaction: DirectoryTransaction, token: str) -> Invitation:
        invitation = await transaction.invitation_by_token_hash(tokens.hash_refresh_token(token))
        if invitation is None or not invitation.is_usable():
            raise NotFound(INVITATION_UNUSABLE)
        return invitation


#: Acceptance happens before there is a session, so it runs at platform scope and is
#: narrowed to the invitation's own organization the moment that row is read. Named
#: rather than inlined so the one place this bypass exists is greppable.
_ACCEPTANCE_SCOPE = TenantScope(role="superadmin", organization_id=None)


def _check_logging_defaults(settings: Mapping[str, Any]) -> None:
    """Validate the one key inside ``settings`` that other code reads.

    The blob is otherwise free-form and stays that way — bounding its size is the only
    rule the schema imposes. This key is different because
    :meth:`app.services.gateways.GatewayService.create_gateway` reads it: a default of
    ``{"retention_dayz": 7}`` would be accepted here, ignored there, and discovered when
    somebody noticed their retention had never changed. Validating it on the form that
    writes it turns that into a 422 with the misspelling in it.
    """
    defaults = organization_logging_defaults(settings)
    if defaults:
        merge_config(LoggingConfig, {}, defaults, field="settings.logging_defaults")


def _check_template_defaults(settings: Mapping[str, Any]) -> None:
    """The same rule for task 105's key: a template default the gateway would refuse is
    refused here, on the form that writes it, with the same message."""
    defaults = organization_template_defaults(settings)
    if defaults:
        merge_config(TemplateConfig, {}, defaults, field="settings.template_defaults")


__all__ = [
    "INVITATION_TTL_DAYS",
    "Actor",
    "DirectoryService",
    "InvitationPreview",
    "IssuedInvitation",
    "OrganizationView",
]
