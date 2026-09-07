"""``/api/v1/gateways`` and ``/api/v1/keys`` — published endpoints and their credentials.

Same three-line shape as the directory and model routes: name a capability, take
:data:`CurrentActor`, hand both to the service.

The capability split is the one thing here that is not uniform, and it is deliberate.
Gateway configuration is ``resources:write``, which an ``org_member`` has — SPEC §5.2 puts
connectors and gateways in their remit. Keys are ``keys:manage``, which they do not, because
a key is a bearer credential for the organization's spend and the task 04 matrix restricts
minting and revoking one to ``org_admin`` and above. Listing keys is only a read, so it
rides on ``org:read``: the response has no secret in it.

``DELETE /keys/{id}`` is a top-level route rather than nested under its gateway. The key id
is unique and already identifies its gateway, and the service scopes it through that
gateway — so a nested path would add a segment the caller has to get right and the server
would have to check for consistency, which is two ways to be wrong instead of none.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.control.deps import (
    CurrentActor,
    get_gateway_service,
    get_memory_preview,
    require_capability,
)
from app.schemas.common import Page
from app.schemas.gateway import (
    ApiKeyCreateRequest,
    ApiKeyResponse,
    GatewayCreateRequest,
    GatewayResponse,
    GatewayTestRequest,
    GatewayTestResponse,
    GatewayUpdateRequest,
    IssuedApiKeyResponse,
    MemoryPreviewRequest,
    PromptPreviewResponse,
    RetrievalPreviewResponse,
)
from app.services.gateways import GatewayService
from app.services.memory_preview import MemoryPreview
from app.services.permissions import Capability

router = APIRouter(tags=["gateways"])

_Service = Annotated[GatewayService, Depends(get_gateway_service)]
_Preview = Annotated[MemoryPreview, Depends(get_memory_preview)]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]

_reads = Depends(require_capability(Capability.ORG_READ))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))
_keys = Depends(require_capability(Capability.KEYS_MANAGE))


@router.get("/gateways", dependencies=[_reads])
async def list_gateways(
    actor: CurrentActor,
    service: _Service,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[GatewayResponse]:
    page = await service.list_gateways(actor, cursor=cursor, limit=limit)
    return Page(
        items=[GatewayResponse.of(view) for view in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/gateways", status_code=status.HTTP_201_CREATED, dependencies=[_writes])
async def create_gateway(
    body: GatewayCreateRequest,
    actor: CurrentActor,
    service: _Service,
) -> GatewayResponse:
    return GatewayResponse.of(await service.create_gateway(actor, body.to_draft()))


@router.get("/gateways/{gateway_id}", dependencies=[_reads])
async def get_gateway(
    gateway_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> GatewayResponse:
    return GatewayResponse.of(await service.get_gateway(actor, gateway_id))


@router.patch("/gateways/{gateway_id}", dependencies=[_writes])
async def update_gateway(
    gateway_id: uuid.UUID,
    body: GatewayUpdateRequest,
    actor: CurrentActor,
    service: _Service,
) -> GatewayResponse:
    """Partial. The three config sections are deep-merged, so a form that owns one
    section can save without knowing what the others contain.

    ``slug`` is refused rather than ignored — it is part of a URL clients have deployed.
    """
    return GatewayResponse.of(await service.update_gateway(actor, gateway_id, body.to_patch()))


@router.delete(
    "/gateways/{gateway_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_writes],
)
async def delete_gateway(
    gateway_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    """Takes its keys and targets with it. Every deployed client stops working, which is
    why the UI asks for the slug to be typed."""
    await service.delete_gateway(actor, gateway_id)


@router.post("/gateways/{gateway_id}/test", dependencies=[_writes])
async def test_gateway(
    gateway_id: uuid.UUID,
    body: GatewayTestRequest,
    actor: CurrentActor,
    service: _Service,
) -> GatewayTestResponse:
    """Send a probe completion through the real proxy path.

    Returns the assembled prompt as well as the answer, which is the part that makes this
    a debugging tool rather than a health check: it shows what the system context became
    without waiting for real traffic to appear in the request log.
    """
    return GatewayTestResponse.of(
        await service.test_gateway(actor, gateway_id, message=body.message)
    )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------
#
# Both are reads — they retrieve, they assemble, they store nothing — but both sit behind
# ``resources:write`` rather than ``org:read``. Two reasons, and the second is the real
# one. They return chunk *text* from the organization knowledge base, which is content a
# read-only role is not otherwise shown anywhere in this UI; and they accept an unsaved
# configuration, which makes them the same surface as the editor they live in.


@router.post("/gateways/{gateway_id}/try-retrieval", dependencies=[_writes])
async def try_retrieval(
    gateway_id: uuid.UUID,
    body: MemoryPreviewRequest,
    actor: CurrentActor,
    preview: _Preview,
) -> RetrievalPreviewResponse:
    """Which chunks this question would inject, at what score, and which survive the
    token budget.

    The same retriever a live request uses, so the scores here are the scores there.
    """
    return RetrievalPreviewResponse.of(
        await preview.try_retrieval(
            actor, gateway_id, query=body.query, memory_config=body.memory_config
        )
    )


@router.post("/gateways/{gateway_id}/prompt-preview", dependencies=[_writes])
async def prompt_preview(
    gateway_id: uuid.UUID,
    body: MemoryPreviewRequest,
    actor: CurrentActor,
    preview: _Preview,
) -> PromptPreviewResponse:
    """The fully assembled system message for a sample question, layer by layer.

    Distinct from ``/test``, which sends a real completion and reports what the provider
    received. This one costs a retrieval and no tokens, so it is the one you press while
    editing; ``/test`` is the one that proves the whole path.
    """
    return PromptPreviewResponse.of(
        await preview.preview_prompt(
            actor, gateway_id, message=body.query, memory_config=body.memory_config
        )
    )


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


@router.get("/gateways/{gateway_id}/keys", dependencies=[_reads])
async def list_keys(
    gateway_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> list[ApiKeyResponse]:
    """Not paginated: a gateway holds tens of keys, not thousands, and the screen shows
    them all at once. The cap in the service is what keeps that true."""
    return [ApiKeyResponse.of(key) for key in await service.list_keys(actor, gateway_id)]


@router.post(
    "/gateways/{gateway_id}/keys",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_keys],
)
async def create_key(
    gateway_id: uuid.UUID,
    body: ApiKeyCreateRequest,
    actor: CurrentActor,
    service: _Service,
) -> IssuedApiKeyResponse:
    """The only response in this API containing a usable secret, and the only time this
    one exists. Only ``sha256(secret)`` is stored, so it cannot be shown again."""
    return IssuedApiKeyResponse.of(
        await service.create_key(actor, gateway_id, name=body.name, expires_at=body.expires_at)
    )


@router.delete("/keys/{key_id}", dependencies=[_keys])
async def revoke_key(
    key_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> ApiKeyResponse:
    """Soft. The row survives so the request log keeps a reference that resolves, and the
    next request with this key is refused — key lookup is never cached."""
    return ApiKeyResponse.of(await service.revoke_key(actor, key_id))


__all__ = ["router"]
