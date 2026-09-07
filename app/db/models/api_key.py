"""Data-plane API keys.

Keys are gateway-scoped (SPEC §5.1). Only ``sha256(secret)`` is stored; ``prefix`` is the
display form the UI can show forever. The row id is the ``key_id`` embedded in the token,
which is what makes authentication a single primary-key lookup — see ``app/core/keys.py``.

Revocation is a timestamp, never a delete. Task 07's request logs reference ``api_key_id``,
and a log line that cannot say which key made the call is worth less than the row it saved.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ApiKey(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "api_keys"

    gateway_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("gateways.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    # Written at most once a minute per key, from a Redis-gated background task, so a
    # hot key does not turn every completion into a database write. See
    # ``app.services.api_keys.LastUsedRecorder``.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Optional. A key with a date in the past authenticates no differently from one that
    #: was revoked, and says so for the same reason: the caller holds it, so telling them
    #: why it stopped working discloses nothing.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
