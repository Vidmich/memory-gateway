"""Upstream models — a provider endpoint plus the credentials and defaults to call it.

Enumerated columns are ``VARCHAR`` with a CHECK constraint rather than a PostgreSQL
``ENUM``: adding ``bedrock`` or ``vertex`` later is then a one-line constraint swap
instead of an ``ALTER TYPE`` that cannot run inside a transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

SCOPES = ("global", "org")
DIALECTS = ("openai", "anthropic")
AUTH_TYPES = ("bearer", "api_key_header", "azure", "none")

DEFAULT_TIMEOUT_SECONDS = 60


class UpstreamModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "upstream_models"
    __table_args__ = (
        CheckConstraint("scope IN ('global', 'org')", name="scope_is_known"),
        CheckConstraint("dialect IN ('openai', 'anthropic')", name="dialect_is_known"),
        CheckConstraint(
            "auth_type IN ('bearer', 'api_key_header', 'azure', 'none')",
            name="auth_type_is_known",
        ),
        CheckConstraint("timeout_seconds > 0", name="timeout_is_positive"),
        # A global model belongs to nobody; an org model must name its owner. Enforced in
        # the database because SPEC §5.3 makes this an isolation boundary, not a nicety.
        CheckConstraint(
            "(scope = 'global' AND organization_id IS NULL)"
            " OR (scope = 'org' AND organization_id IS NOT NULL)",
            name="scope_matches_organization",
        ),
        UniqueConstraint("organization_id", "name", name="uq_upstream_models_organization_id_name"),
        # NULLs never collide in a UNIQUE constraint, so global names need their own
        # partial index to stay unique.
        Index(
            "uq_upstream_models_global_name",
            "name",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="org")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    dialect: Mapped[str] = mapped_column(String(32), nullable=False, default="openai")
    upstream_model_id: Mapped[str] = mapped_column(Text, nullable=False)

    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="bearer")
    # Envelope-encrypted (app/core/crypto.py). Never returned by any API; SPEC §5.4.
    credential_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    extra_headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    system_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_TIMEOUT_SECONDS
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
