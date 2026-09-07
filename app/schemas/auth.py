"""Request and response bodies for ``/api/v1/auth``.

These are also the contract the frontend's generated client is built from, so field names
and optionality here are load-bearing on the other side of the wire.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.passwords import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
from app.db.models import Organization, User


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Not validated for length on the way in: rejecting a short password at *login*
    # would tell an attacker that the account's password is short.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)
    #: Longer refresh TTL and a persistent cookie. Off by default.
    remember: bool = False


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)


class OrganizationSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str

    @classmethod
    def of(cls, organization: Organization) -> OrganizationSummary:
        return cls(id=organization.id, name=organization.name, slug=organization.slug)


class UserSummary(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    status: str
    last_login_at: datetime | None = None
    #: ``None`` for a superadmin, who belongs to the platform rather than a tenant.
    organization: OrganizationSummary | None = None

    @classmethod
    def of(cls, user: User, organization: Organization | None) -> UserSummary:
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            role=user.role,
            status=user.status,
            last_login_at=user.last_login_at,
            organization=OrganizationSummary.of(organization) if organization else None,
        )


class SessionResponse(BaseModel):
    """What login and refresh both return.

    The refresh token is deliberately absent: it lives in an httpOnly cookie the
    JavaScript never sees, which is the whole point of splitting the two.
    """

    access_token: str
    token_type: str = "bearer"
    #: Absolute, so the client can schedule a refresh instead of waiting for a 401.
    #: ``expires_in`` is included too because clock skew makes the absolute form
    #: unreliable on its own.
    expires_at: datetime
    expires_in: int
    user: UserSummary
