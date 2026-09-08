"""Organizations — the tenant boundary.

``settings`` holds org-level defaults other parts of the system read: logging defaults
under ``logging_defaults`` and the distillation model (task 13). It is a JSONB column
rather than a widening list of columns because each of those would otherwise need a
migration on a populated table to add one nullable default.

Retention deliberately did *not* end up here. It is a platform **ceiling** rather than an
organization default — a maximum an organization may be stricter than and never longer —
so it lives in ``platform_settings`` where an org admin cannot raise it, and the number a
gateway actually gets is its own capped by that.

The cost of that flexibility is that nothing in the database constrains its shape, so
:mod:`app.schemas.directory` bounds it on the way in — an org admin should not have an
unbounded write primitive.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: ``deleting`` is task 17's soft delete: the organization keeps every row it has and
#: stops being usable, and a scheduled pass destroys it once ``purge_after`` has passed.
#: A status rather than a nullable ``deleted_at`` alone, because every gateway and every
#: login already branches on this column — a second flag would be a second thing each of
#: them has to remember to check.
ORGANIZATION_STATUSES = ("active", "suspended", "deleting")


class Organization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended', 'deleting')",
            name="status_is_known",
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: When the destructive pass may run. Set with ``status = 'deleting'`` and cleared by
    #: cancelling. The two are kept together rather than derived from one another, so that
    #: "scheduled for deletion" and "when" are one write and cannot come apart.
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        """A suspended *or deleting* organization keeps its data and stops being usable:
        its members cannot sign in, and its gateways stop serving (task 06 reads this).

        One property for both because the effect is identical, and the difference — that
        one of them is on its way out — belongs on the screen rather than in the check
        every request makes."""
        return self.status == "active"
