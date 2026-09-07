"""Control-plane users.

``organization_id`` is nullable because a superadmin belongs to the platform rather than
to a tenant (SPEC §5.2). The check constraint ties the two together so the "which org am
I acting in" question always has exactly one answer, rather than depending on a
convention that some future endpoint forgets.

``role`` carries all four values from SPEC §5.2 even though nothing enforces them until
task 04. Widening a ``CHECK`` on a populated table means a migration and a validation
scan; declaring the full domain now costs nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ROLES = ("superadmin", "org_admin", "org_member", "org_viewer")
USER_STATUSES = ("active", "invited", "suspended")

#: Roles that may act only inside their own organization.
ORG_ROLES = ("org_admin", "org_member", "org_viewer")


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('superadmin', 'org_admin', 'org_member', 'org_viewer')",
            name="role_is_known",
        ),
        CheckConstraint(
            "status IN ('active', 'invited', 'suspended')",
            name="status_is_known",
        ),
        CheckConstraint(
            "(role = 'superadmin' AND organization_id IS NULL)"
            " OR (role <> 'superadmin' AND organization_id IS NOT NULL)",
            name="role_matches_organization",
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # CITEXT rather than lower-casing in application code: it makes case-insensitive
    # uniqueness a property of the column, so no future insert path can bypass it.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def can_log_in_with_password(self) -> bool:
        """False for an invited user who has not set one, and for an OIDC-only user
        once task 16's provider lands — hence the nullable column."""
        return self.password_hash is not None
