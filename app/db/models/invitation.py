"""Invitations — how a person joins an organization.

Only ``sha256(token)`` is stored, for the same reason as a refresh token: the value is
256 bits of CSPRNG output, so there is no dictionary to attack and a slow hash would buy
nothing, but a leaked database dump must not contain working invitation links.

That has a visible consequence. The link cannot be shown again after creation, because
nothing here can reconstruct it. "Resend" therefore *rotates* the token — which is what
resending an invitation means anyway, and it invalidates a link that may have gone to the
wrong address.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: A superadmin belongs to the platform, so there is no organization to invite them into.
INVITABLE_ROLES = ("org_admin", "org_member", "org_viewer")


class Invitation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(
            "role IN ('org_admin', 'org_member', 'org_viewer')",
            name="role_is_invitable",
        ),
        # One live invitation per address per organization. Partial, so an accepted or
        # expired one does not block a fresh invite to someone who left and came back.
        Index(
            "uq_invitations_pending_email",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL"),
        ),
        # `organization_id` first, so the index is usable for the scoped list query and
        # so row-level security stays mechanical to add later (task 04 notes).
        Index("ix_invitations_organization_id_created_at", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: Nullable so removing the admin who sent it does not delete the invitation.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(UTC))

    def is_usable(self, *, now: datetime | None = None) -> bool:
        """Single-use and time-limited: both halves are checked in one place so no
        caller can test one and forget the other."""
        return not self.is_accepted and not self.is_expired(now=now)
