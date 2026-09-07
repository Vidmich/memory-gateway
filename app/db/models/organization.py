"""Organizations — the tenant boundary.

``settings`` holds org-level defaults other parts of the system read: logging defaults
under ``logging_defaults`` today, the distillation model (task 13) and retention
(task 17) later. It is a JSONB column rather
than a widening list of columns because each of those tasks would otherwise need a
migration on a populated table to add one nullable default.

The cost of that flexibility is that nothing in the database constrains its shape, so
:mod:`app.schemas.directory` bounds it on the way in — an org admin should not have an
unbounded write primitive.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ORGANIZATION_STATUSES = ("active", "suspended")


class Organization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="status_is_known",
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    @property
    def is_active(self) -> bool:
        """A suspended organization keeps its data and stops being usable: its members
        cannot sign in, and its gateways stop serving (task 06 reads this)."""
        return self.status == "active"
