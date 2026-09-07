"""Organizations.

Deliberately minimal: task 04 owns tenancy. It exists now only so every table added
from here on can carry a real ``organization_id`` foreign key. Retrofitting one across a
populated schema is an order of magnitude more work than declaring it up front.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, Text
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
