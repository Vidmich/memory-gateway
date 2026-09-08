"""``/api/v1/distillation`` — the settings, the health chart, and the manual pass.

Three capabilities, and the split is the point.

**Reading the settings needs only ``org:read``.** They are the answer to "why has the
assistant not learned anything about this customer", which is a question the person on
support duty asks, and putting it behind the permission to reconfigure production would
mean they cannot.

**Changing them needs ``org:administer``.** The distillation model is a spending decision
and the daily cap is a spending limit; they belong with the rest of the organization's
profile and defaults, on the same screen and with the same audience.

**Running a pass needs ``resources:write``.** It writes memory — the same memory a person
can write by hand from the browser — so it takes the capability that writing a fact by hand
takes. It is also synchronous and calls a provider, which is why it is bounded to a handful
of threads and reports what it did rather than answering 202: the whole reason the button
exists is that "it is queued" is exactly the answer that leaves somebody none the wiser.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.control.deps import CurrentActor, get_distillation_service, require_capability
from app.schemas.distillation import (
    DistillationSettingsRequest,
    DistillationSettingsResponse,
)
from app.schemas.monitoring import ManualPassResponse, MemoryHealthResponse
from app.services.distillation_service import (
    DEFAULT_HEALTH_DAYS,
    MAX_HEALTH_DAYS,
    DistillationService,
)
from app.services.permissions import Capability

router = APIRouter(tags=["distillation"])

_Service = Annotated[DistillationService, Depends(get_distillation_service)]

_reads = Depends(require_capability(Capability.ORG_READ))
_administers = Depends(require_capability(Capability.ORG_ADMINISTER))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))


@router.get("/distillation", dependencies=[_reads])
async def get_distillation_settings(
    actor: CurrentActor, service: _Service
) -> DistillationSettingsResponse:
    """What this organization has decided about writing its own memory, plus today's spend.

    The usage figure comes from the same table the cap is enforced against, so the number
    on the screen is the number that will refuse the next pass.
    """
    return await service.settings(actor)


@router.patch("/distillation", dependencies=[_administers])
async def update_distillation_settings(
    body: DistillationSettingsRequest, actor: CurrentActor, service: _Service
) -> DistillationSettingsResponse:
    """A partial update. Only the fields present in the body are changed.

    ``model_id: null`` is a value, not an omission: it means "go back to the platform
    default", and a body that could not express it would make clearing the selector
    impossible.
    """
    return await service.update(actor, body.patch())


@router.get("/distillation/health", dependencies=[_reads])
async def get_memory_health(
    actor: CurrentActor,
    service: _Service,
    days: Annotated[int, Query(ge=1, le=MAX_HEALTH_DAYS)] = DEFAULT_HEALTH_DAYS,
) -> MemoryHealthResponse:
    """SPEC §10.1's memory health, plus the two rates that catch a silent failure.

    A dedupe rate near 100% means passes are succeeding and producing nothing; a
    supersession rate near zero on an established user means contradictions are not being
    caught. Both look like health from every other angle, which is why they are here rather
    than left to be inferred from the fact count.
    """
    return MemoryHealthResponse.of(await service.health(actor, days=days))


@router.post("/end-users/{end_user_id}/distil", dependencies=[_writes])
async def distil_now(
    end_user_id: uuid.UUID, actor: CurrentActor, service: _Service
) -> ManualPassResponse:
    """Run a distillation pass over this person's pending conversations, now.

    The real pass — same extractor, same threshold, same reconciliation as the background
    job — so what it reports is what the scheduled one would have done. It answers the
    question the debounce makes hard to ask: is this working, and if not, why not.
    """
    return ManualPassResponse.of(await service.distil_now(actor, end_user_id))
