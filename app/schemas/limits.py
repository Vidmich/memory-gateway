"""Response shapes for the Limits screen and the dashboard's warning card.

Two things are deliberate here.

**Configured and enforced are both sent.** They are usually identical, and when they are
not the difference *is* the message: an org typed 5000 and the platform caps this gateway
at 600. Sending only the enforced value would make the form look like it had lost the
save; sending only the configured value would make the utilisation bar disagree with the
429s.

**The ceilings are sent whether or not they apply.** A gateway on the organization's own
models is unaffected by them today and will not be tomorrow if somebody points it at a
global model — and that is exactly the moment the number matters, which is before the
change rather than after it.
"""

from __future__ import annotations

import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict

from app.schemas.gateway_config import LimitsConfig, Quota
from app.services.limits import Ceilings
from app.services.limits_service import GatewayLimits, LimitUsage, Pressure

_CONFIG = ConfigDict(extra="forbid")


class QuotaResponse(BaseModel):
    """SPEC §11's four caps. ``null`` is unlimited, at every level."""

    model_config = _CONFIG

    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    requests_per_day: int | None = None
    concurrent_requests: int | None = None

    @classmethod
    def of(cls, quota: Quota | Ceilings) -> Self:
        return cls(
            requests_per_minute=quota.requests_per_minute,
            tokens_per_minute=quota.tokens_per_minute,
            requests_per_day=quota.requests_per_day,
            concurrent_requests=quota.concurrent_requests,
        )


class LimitUsageResponse(BaseModel):
    """One cap's live bar."""

    model_config = _CONFIG

    limit: str
    scope: str
    value: int
    used: int
    remaining: int
    #: Seconds until the window rolls. ``0`` for ``concurrent_requests``, which has no
    #: window — a slot frees when a request finishes, not at a time anyone can name.
    reset_seconds: int
    utilization: float
    capped: bool

    @classmethod
    def of(cls, usage: LimitUsage) -> Self:
        return cls(
            limit=usage.limit,
            scope=usage.scope,
            value=usage.value,
            used=usage.used,
            remaining=usage.remaining,
            reset_seconds=usage.reset_seconds,
            utilization=usage.utilization,
            capped=usage.capped,
        )


class GatewayLimitsResponse(BaseModel):
    model_config = _CONFIG

    gateway_id: uuid.UUID
    slug: str
    name: str
    configured: QuotaResponse
    configured_per_end_user: QuotaResponse
    enforced: QuotaResponse
    enforced_per_end_user: QuotaResponse
    #: Gateway-scope limits the platform ceiling changed. Named rather than implied, so
    #: the editor can put the explanation next to the input rather than in a banner.
    capped: list[str]
    ceilings: QuotaResponse
    #: Whether this gateway reaches a model on the operator's own credential, which is
    #: what makes the ceilings above apply.
    global_models: bool
    usage: list[LimitUsageResponse]

    @classmethod
    def of(cls, view: GatewayLimits) -> Self:
        configured: LimitsConfig = view.configured
        return cls(
            gateway_id=view.gateway_id,
            slug=view.slug,
            name=view.name,
            configured=QuotaResponse.of(configured.gateway),
            configured_per_end_user=QuotaResponse.of(configured.per_end_user),
            enforced=QuotaResponse.of(view.enforced.gateway),
            enforced_per_end_user=QuotaResponse.of(view.enforced.per_end_user),
            capped=list(view.enforced.capped),
            ceilings=QuotaResponse.of(view.ceilings),
            global_models=view.global_models,
            usage=[LimitUsageResponse.of(entry) for entry in view.usage],
        )


class PressureResponse(BaseModel):
    """One gateway currently running close to one of its caps."""

    model_config = _CONFIG

    gateway_id: uuid.UUID
    slug: str
    name: str
    worst: LimitUsageResponse

    @classmethod
    def of(cls, pressure: Pressure) -> Self:
        return cls(
            gateway_id=pressure.gateway_id,
            slug=pressure.slug,
            name=pressure.name,
            worst=LimitUsageResponse.of(pressure.worst),
        )


class PressureListResponse(BaseModel):
    """Deliberately not a page. The question is "is anything under pressure right now",
    and an answer split across pages is a different question."""

    model_config = _CONFIG

    items: list[PressureResponse]


__all__ = [
    "GatewayLimitsResponse",
    "LimitUsageResponse",
    "PressureListResponse",
    "PressureResponse",
    "QuotaResponse",
]
