"""Request and response bodies for organizations, members and invitations.

These are the contract the frontend's generated client is built from, so field names and
optionality are load-bearing on the other side of the wire.

Two shapes to note. Every list endpoint answers ``{items, next_cursor}`` (SPEC §12.2), so
:class:`Page` is generic and every list response is one instantiation of it — a client
that can page one list can page all of them. And every update body distinguishes "not
sent" from "set to null" by defaulting to ``None`` and being applied only when present,
which is what makes ``PATCH`` partial rather than a ``PUT`` in disguise.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.passwords import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
from app.db.models import Invitation, Organization, User
from app.db.models.invitation import INVITABLE_ROLES
from app.db.models.organization import ORGANIZATION_STATUSES
from app.schemas.common import Page
from app.services.directory import InvitationPreview, IssuedInvitation, OrganizationView

#: Lower-case, hyphen-separated, no leading or trailing hyphen. The slug appears in the
#: public gateway URL (``/g/{slug}/v1``), so it has to survive being typed, copied into a
#: shell, and pasted into a config file.
SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

Slug = Annotated[str, Field(min_length=2, max_length=63, pattern=SLUG_PATTERN)]
Name = Annotated[str, Field(min_length=1, max_length=200)]

#: An org admin can write anything into ``settings``, so it needs a ceiling. 16 KiB is
#: far more than the handful of defaults later tasks put there, and small enough that it
#: cannot be used as free storage or to make a row expensive to read.
MAX_SETTINGS_BYTES = 16 * 1024


# ---------------------------------------------------------------------------
# organizations
# ---------------------------------------------------------------------------


class OrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    settings: dict[str, Any]
    created_at: datetime
    #: Present on list responses; both are 0 for an organization with nothing in it.
    member_count: int = 0
    gateway_count: int = 0

    @classmethod
    def of(cls, organization: Organization, *, members: int = 0, gateways: int = 0) -> Self:
        return cls(
            id=organization.id,
            name=organization.name,
            slug=organization.slug,
            status=organization.status,
            settings=dict(organization.settings or {}),
            created_at=organization.created_at,
            member_count=members,
            gateway_count=gateways,
        )

    @classmethod
    def of_view(cls, view: OrganizationView) -> Self:
        return cls.of(view.organization, members=view.member_count, gateways=view.gateway_count)


class OrganizationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    slug: Slug


class OrganizationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    slug: Slug | None = None
    #: Platform-only; the route refuses it from an org admin rather than ignoring it,
    #: because silently dropping a field the caller sent is worse than saying no.
    status: str | None = None
    settings: dict[str, Any] | None = None

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in ORGANIZATION_STATUSES:
            raise ValueError(f"must be one of {', '.join(ORGANIZATION_STATUSES)}")
        return value

    @field_validator("settings")
    @classmethod
    def _bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        encoded = json.dumps(value)
        if len(encoded.encode("utf-8")) > MAX_SETTINGS_BYTES:
            raise ValueError(f"must serialize to at most {MAX_SETTINGS_BYTES} bytes")
        return value


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


class MemberResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    status: str
    last_login_at: datetime | None = None
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> Self:
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            status=user.status,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
        )


class MemberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    status: str | None = None

    @field_validator("role")
    @classmethod
    def _invitable_role(cls, value: str | None) -> str | None:
        # `superadmin` is absent on purpose: promoting a tenant user to a platform
        # account through the members API would be a privilege escalation with no
        # corresponding screen, and the CHECK constraint would reject the row anyway.
        if value is not None and value not in INVITABLE_ROLES:
            raise ValueError(f"must be one of {', '.join(INVITABLE_ROLES)}")
        return value

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str | None) -> str | None:
        if value is not None and value not in ("active", "suspended"):
            raise ValueError("must be one of active, suspended")
        return value


# ---------------------------------------------------------------------------
# invitations
# ---------------------------------------------------------------------------


class InvitationResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    role: str
    #: Derived, not stored: ``pending``, ``accepted`` or ``expired``. One field the UI can
    #: put through the same `StatusBadge` as everything else.
    status: str
    expires_at: datetime
    accepted_at: datetime | None = None
    created_at: datetime

    @classmethod
    def of(cls, invitation: Invitation) -> Self:
        return cls(
            id=invitation.id,
            organization_id=invitation.organization_id,
            email=invitation.email,
            role=invitation.role,
            status=invitation_status(invitation),
            expires_at=invitation.expires_at,
            accepted_at=invitation.accepted_at,
            created_at=invitation.created_at,
        )


class IssuedInvitationResponse(BaseModel):
    """The one response that carries the link.

    Only the hash is stored, so this is the single moment it can be shown; the list
    endpoint has no ``accept_url`` field to leak later.
    """

    invitation: InvitationResponse
    accept_url: str

    @classmethod
    def of(cls, issued: IssuedInvitation, *, base_url: str) -> Self:
        return cls(
            invitation=InvitationResponse.of(issued.invitation),
            accept_url=issued.url(base_url),
        )


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: str

    @field_validator("role")
    @classmethod
    def _invitable_role(cls, value: str) -> str:
        if value not in INVITABLE_ROLES:
            raise ValueError(f"must be one of {', '.join(INVITABLE_ROLES)}")
        return value


class InvitationPreviewResponse(BaseModel):
    """What an unauthenticated visitor may see about a link they hold.

    The organization's *name* is here because the page has to say which organization is
    being joined; nothing else about it is, and there is no id.
    """

    email: str
    role: str
    organization_name: str
    expires_at: datetime

    @classmethod
    def of(cls, preview: InvitationPreview) -> Self:
        return cls(
            email=preview.email,
            role=preview.role,
            organization_name=preview.organization_name,
            expires_at=preview.expires_at,
        )


class InvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)


def invitation_status(invitation: Invitation) -> str:
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.expires_at <= datetime.now(UTC):
        return "expired"
    return "pending"


#: Re-exported so a caller reading directory responses has one import.
__all__ = [
    "InvitationAcceptRequest",
    "InvitationCreateRequest",
    "InvitationPreviewResponse",
    "InvitationResponse",
    "IssuedInvitationResponse",
    "MemberResponse",
    "MemberUpdateRequest",
    "OrganizationCreateRequest",
    "OrganizationResponse",
    "OrganizationUpdateRequest",
    "Page",
    "invitation_status",
]
