"""Gateways and their upstream targets.

A gateway is the customer-facing endpoint: ``/g/{slug}/v1/*``. Task 02 creates exactly
one target per gateway; ``priority`` and ``weight`` exist now so task 08 can turn the
list into a failover chain or an A/B split without a schema change on a live table.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.upstream_model import UpstreamModel


class Gateway(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "gateways"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    system_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    param_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    targets: Mapped[list[GatewayTarget]] = relationship(
        back_populates="gateway",
        cascade="all, delete-orphan",
        order_by="GatewayTarget.priority",
    )


class GatewayTarget(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "gateway_targets"
    __table_args__ = (
        UniqueConstraint("gateway_id", "upstream_model_id", name="uq_gateway_targets_pair"),
        CheckConstraint("weight >= 0", name="weight_is_not_negative"),
    )

    gateway_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("gateways.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Restricted rather than cascaded: deleting a model that a live gateway routes to
    # would silently break that endpoint, so the delete must fail and be dealt with.
    upstream_model_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("upstream_models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    gateway: Mapped[Gateway] = relationship(back_populates="targets")
    upstream_model: Mapped[UpstreamModel] = relationship(lazy="joined")
