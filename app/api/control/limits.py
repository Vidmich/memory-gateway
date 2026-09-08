"""``/api/v1`` — reading SPEC §11's limits and how much of them is spent.

Reads only. The limits themselves are *written* through the gateway editor, under
``PATCH /gateways/{id}``, because they are one section of a gateway's configuration and
splitting the write across two endpoints would mean two save buttons on one form and two
places for the platform ceiling to be enforced.

Both routes need only ``org:read``: a utilisation bar is diagnostic, and the person
answering "why is our integration getting 429s" is often not the person allowed to raise
the limit.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.control.deps import CurrentActor, get_limits_service, require_capability
from app.schemas.limits import (
    GatewayLimitsResponse,
    PressureListResponse,
    PressureResponse,
)
from app.services.limits_service import LimitsService
from app.services.permissions import Capability

router = APIRouter(tags=["limits"])

_Service = Annotated[LimitsService, Depends(get_limits_service)]

_reads = Depends(require_capability(Capability.ORG_READ))


@router.get("/gateways/{gateway_id}/limits", dependencies=[_reads])
async def get_gateway_limits(
    gateway_id: uuid.UUID, actor: CurrentActor, service: _Service
) -> GatewayLimitsResponse:
    """This gateway's caps, what is actually enforced, and how much is spent right now.

    The usage is read live from the same buckets a request is checked against, so a bar
    at 100% and a 429 in the client's log are the same fact rather than two systems that
    usually agree.
    """
    return GatewayLimitsResponse.of(await service.gateway(actor, gateway_id))


@router.get("/limits/pressure", dependencies=[_reads])
async def get_limit_pressure(actor: CurrentActor, service: _Service) -> PressureListResponse:
    """Gateways currently past 80% of one of their caps, worst first.

    The dashboard's warning card. Usually empty, which is why it is one request for the
    whole organization rather than one per gateway.
    """
    return PressureListResponse(
        items=[PressureResponse.of(item) for item in await service.pressure(actor)]
    )
