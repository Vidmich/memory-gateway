"""Gateways and their upstream targets.

A gateway is the customer-facing endpoint: ``/g/{slug}/v1/*``. The slug is in that URL,
which is why it is globally unique and why task 06 makes it immutable — renaming it would
break every deployed client from a settings form, silently and instantly.

The ``*_config`` columns are JSONB rather than columns per setting. Their contents
are owned by tasks 10, 07, 14 and 105 respectively, and each will add fields; a blob keeps
that from being a migration per knob on a live table. They are not free-form, though —
:mod:`app.schemas.gateway_config` validates every one on the way in and fills defaults on
the way out, so a row written before a field existed still loads.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.upstream_model import UpstreamModel

#: SPEC §8.1. All three are served; :mod:`app.services.routing` turns a mode and the
#: ordered ``gateway_targets`` rows into an attempt list, and
#: :mod:`app.services.gateways` refuses a chain that contradicts the mode.
ROUTING_MODES = ("single", "failover", "ab_split")

MIN_SLUG_LENGTH = 3
MAX_SLUG_LENGTH = 63


class Gateway(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "gateways"
    __table_args__ = (
        CheckConstraint(
            "routing_mode IN ('single', 'failover', 'ab_split')", name="routing_mode_is_known"
        ),
        # The shape of a public URL segment, enforced where it cannot be bypassed. The
        # service rejects the same values with a readable message; this is what stops a
        # migration, a script, or a future endpoint from writing one that breaks routing.
        CheckConstraint(r"slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])$'", name="slug_is_url_safe"),
        Index("ix_gateways_organization_id_id", "organization_id", "id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(MAX_SLUG_LENGTH), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    routing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="single", server_default="single"
    )

    system_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    param_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: Parameters the client may not change. Applied *after* the client's own values —
    #: see ``app.services.params.resolve_params`` — which is what makes them a cap rather
    #: than another default.
    locked_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    #: Owned by tasks 10, 07 and 14. Created here with schema-validated defaults so those
    #: tasks add fields to a Pydantic model instead of columns to a live table.
    memory_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    logging_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    limits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: Task 105. The text the gateway writes around documents, memory and answers, as
    #: nine templates; ``{}`` renders exactly what SPEC §7 prints.
    template_config: Mapped[dict[str, Any]] = mapped_column(
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
    #: Position in a ``failover`` chain, and which single target ``single`` uses. Zero
    #: is tried first.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: A percentage, read only by ``ab_split``. Stored whatever the mode, so switching a
    #: gateway to ``single`` to debug something and back again does not lose the split.
    #: The service refuses a saved A/B chain whose weights do not total 100; the router
    #: still divides by the real total, so a chain written around the service — or one a
    #: disabled model dropped out of — splits in proportion rather than losing traffic.
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    gateway: Mapped[Gateway] = relationship(back_populates="targets")
    upstream_model: Mapped[UpstreamModel] = relationship(lazy="joined")
