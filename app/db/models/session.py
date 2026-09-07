"""Refresh-token sessions.

One row per *issued refresh token*, not one per login. Rotation inserts a new row and
marks the old one replaced, so the table is an append-only chain per login — which is
what makes theft detectable.

``family_id`` is the login. If a token that has already been rotated is presented again,
either the client replayed it or someone stole it; there is no way to tell which, so the
whole family is revoked and the user has to log in again (OAuth 2.1 §4.14.2 recommends
exactly this). That is the entire reason for the two columns SPEC §14 does not list.

The class is ``UserSession`` because ``Session`` in a SQLAlchemy codebase means something
else entirely; the table keeps the spec's name.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Why a session row stopped being usable. Recorded rather than inferred: "the user
#: logged out" and "we detected a stolen token" want very different responses from an
#: operator reading the table.
REVOKE_REASONS = ("logout", "rotated", "reuse_detected", "password_change", "expired")


class UserSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_family_id", "family_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Groups every token issued by one login. See the module docstring.
    family_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: "Remember me". Decided at login and carried through every rotation, so a session
    #: the user asked not to persist cannot quietly become a persistent one.
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Set when this token was exchanged for a new one. A *second* presentation of a
    #: replaced token is the theft signal.
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # INET, not text: a session list that cannot be queried by subnet is much less useful
    # to whoever is investigating an incident.
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    def is_usable(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return self.revoked_at is None and self.replaced_at is None and self.expires_at > moment
