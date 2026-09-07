"""``/api/v1/models`` — the upstream model catalog.

Same three-line shape as the directory routes: name a capability, take
:data:`CurrentActor`, hand both to the service. No route here takes an ``organization_id``
— which organization a model belongs to is decided by the scope on the way in, and read
off the row on the way out.

The capability gates are coarse on purpose. Every write route asks for
``resources:write``, and the two rules a role check cannot express — only a platform
administrator may write the global catalog, and nobody may edit a model they do not own —
live in :class:`~app.services.catalog.CatalogService`, where they are one rule each
rather than one per route.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.control.deps import CurrentActor, get_catalog_service, require_capability
from app.schemas.catalog import (
    ModelCreateRequest,
    ModelResponse,
    ModelTestRequest,
    ModelUpdateRequest,
    ProbeResponse,
)
from app.schemas.common import Page
from app.services.catalog import CatalogService
from app.services.permissions import Capability

router = APIRouter(tags=["models"])

_Service = Annotated[CatalogService, Depends(get_catalog_service)]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]

_reads = Depends(require_capability(Capability.ORG_READ))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))


@router.get("/models", dependencies=[_reads])
async def list_models(
    actor: CurrentActor,
    service: _Service,
    scope: Annotated[str | None, Query(pattern="^(global|org)$")] = None,
    enabled: bool | None = None,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[ModelResponse]:
    """The caller's own models plus the global catalog, newest first.

    One endpoint for both tabs: ``?scope=global`` and ``?scope=org`` are a filter over the
    same list rather than two shapes, so the client pages them identically and a model
    cannot appear in one view and not the other.
    """
    page = await service.list_models(
        actor, scope_filter=scope, enabled=enabled, cursor=cursor, limit=limit
    )
    return Page(
        items=[ModelResponse.of(view) for view in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/models", status_code=status.HTTP_201_CREATED, dependencies=[_writes])
async def create_model(
    body: ModelCreateRequest,
    actor: CurrentActor,
    service: _Service,
) -> ModelResponse:
    return ModelResponse.of(await service.create_model(actor, body.to_draft()))


@router.post("/models/test", dependencies=[_writes])
async def test_draft(
    body: ModelTestRequest,
    actor: CurrentActor,
    service: _Service,
) -> ProbeResponse:
    """Probe a configuration that has not been saved.

    Declared before ``/models/{model_id}`` so ``test`` is not swallowed as an id — FastAPI
    matches in declaration order, and ``uuid.UUID`` would reject it with a 422 that reads
    like the endpoint is broken.
    """
    return ProbeResponse.of(await service.test_draft(actor, body.to_draft()))


@router.get("/models/{model_id}", dependencies=[_reads])
async def get_model(
    model_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> ModelResponse:
    return ModelResponse.of(await service.get_model(actor, model_id))


@router.patch("/models/{model_id}", dependencies=[_writes])
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdateRequest,
    actor: CurrentActor,
    service: _Service,
) -> ModelResponse:
    """Partial. An omitted ``credential`` keeps the stored one; an explicit ``null``
    clears it. There is no way to read it back, so rotation is replacement."""
    return ModelResponse.of(await service.update_model(actor, model_id, body.to_patch()))


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_writes],
)
async def delete_model(
    model_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    """409 with the referencing gateways named if one points at this model."""
    await service.delete_model(actor, model_id)


@router.post("/models/{model_id}/test", dependencies=[_writes])
async def test_model(
    model_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> ProbeResponse:
    """Send a one-token completion using the model exactly as stored.

    Nothing about the request can be overridden here. A caller who could aim a *stored*
    credential at a URL of their choosing would have a reveal endpoint in all but name.
    """
    return ProbeResponse.of(await service.test_model(actor, model_id))


__all__ = ["router"]
