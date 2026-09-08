"""``/api/v1/end-users`` and ``/api/v1/memory-facts`` — the memory browser (SPEC §12, §13.1).

Same three-line shape as the other control-plane routers: name a capability, take
:data:`CurrentActor`, hand both to the service. No route takes an ``organization_id``.

Two things here are unlike the routers before it.

**Reading a fact needs only the read capability.** These are sentences distilled from an
end user's own conversations, which is more personal than anything else the control plane
shows — and that is an argument for the *audit trail* (task 15) rather than for a higher
bar to look. Someone triaging "the assistant told my customer the wrong thing" needs to
see what it believed, and making that require the permission to reconfigure production
would mean the person answering the ticket cannot.

**Purge is a DELETE that returns a body.** 204 would be the tidier status and the wrong
one: an erasure has to be able to say what it removed — how many facts, how many
transcripts — because "it said it worked" is not the same evidence as "it removed
fourteen facts and nine transcripts", and this is the request somebody will be asked
about later.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.control.deps import CurrentActor, get_end_user_service, require_capability
from app.schemas.common import Page
from app.schemas.end_user import (
    EndUserResponse,
    MemoryFactCreateRequest,
    MemoryFactResponse,
    MemoryFactUpdateRequest,
    MemoryPurgeResponse,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySearchResponse,
    present_fields,
)
from app.services.end_users import EndUserService
from app.services.permissions import Capability

router = APIRouter(tags=["end-users"])

_Service = Annotated[EndUserService, Depends(get_end_user_service)]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]

_reads = Depends(require_capability(Capability.ORG_READ))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))


@router.get("/end-users", dependencies=[_reads])
async def list_end_users(
    actor: CurrentActor,
    service: _Service,
    search: Annotated[str | None, Query(max_length=200)] = None,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[EndUserResponse]:
    """Everyone this organization's gateways have seen, newest first."""
    page = await service.list_end_users(actor, search=search, cursor=cursor, limit=limit)
    return Page(
        items=[EndUserResponse.of(view) for view in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/end-users/{end_user_id}", dependencies=[_reads])
async def get_end_user(
    end_user_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> EndUserResponse:
    return EndUserResponse.of(await service.get_end_user(actor, end_user_id))


@router.get("/end-users/{end_user_id}/memory", dependencies=[_reads])
async def list_memory(
    end_user_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    live_only: Annotated[bool, Query()] = False,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[MemoryFactResponse]:
    """What the assistant believes about this person.

    Superseded and expired facts are included unless ``live_only`` is set: the browser's
    job includes explaining an answer the assistant gave last month, and the fact that
    explains it is usually the one that has since been replaced.
    """
    page = await service.list_facts(
        actor, end_user_id, live_only=live_only, cursor=cursor, limit=limit
    )
    return Page(
        items=[MemoryFactResponse.of(fact) for fact in page.items],
        next_cursor=page.next_cursor,
    )


@router.post("/end-users/{end_user_id}/memory/search", dependencies=[_reads])
async def search_memory(
    end_user_id: uuid.UUID,
    body: MemorySearchRequest,
    actor: CurrentActor,
    service: _Service,
) -> MemorySearchResponse:
    """Semantic search over one person's memory — the same search a request runs.

    A POST rather than a GET because the query is free text that would otherwise sit in a
    URL, and a URL is the one place a query about a named individual should not be: it
    reaches the access log, the browser's history and any proxy in between.
    """
    hits = await service.search_facts(actor, end_user_id, body.query, limit=body.limit)
    return MemorySearchResponse(
        hits=[MemorySearchHit.of(hit) for hit in hits],
        embedding_model=service.embedding_model,
    )


@router.post(
    "/end-users/{end_user_id}/memory",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_writes],
)
async def create_memory_fact(
    end_user_id: uuid.UUID,
    body: MemoryFactCreateRequest,
    actor: CurrentActor,
    service: _Service,
) -> MemoryFactResponse:
    """Write a fact by hand.

    What makes conversation memory demonstrable before task 13's distillation exists, and
    what stays afterwards: "it keeps forgetting we are in the EU" needs an answer that is
    not "wait for the next distillation pass".
    """
    return MemoryFactResponse.of(
        await service.create_fact(
            actor,
            end_user_id,
            text=body.text,
            kind=body.kind,
            confidence=body.confidence,
            expires_at=body.expires_at,
        )
    )


@router.patch("/memory-facts/{fact_id}", dependencies=[_writes])
async def update_memory_fact(
    fact_id: uuid.UUID,
    body: MemoryFactUpdateRequest,
    actor: CurrentActor,
    service: _Service,
) -> MemoryFactResponse:
    """Correct a fact, retract it, or bring it back."""
    return MemoryFactResponse.of(
        await service.update_fact(actor, fact_id, body.to_patch(fields=present_fields(body)))
    )


@router.delete(
    "/memory-facts/{fact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_writes],
)
async def delete_memory_fact(
    fact_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> None:
    """The row and its vector. Synchronous: it is two small deletes and somebody is
    looking at the fact they just removed."""
    await service.delete_fact(actor, fact_id)


@router.delete("/end-users/{end_user_id}/memory", dependencies=[_writes])
async def purge_memory(
    end_user_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    include_transcripts: Annotated[bool, Query()] = False,
) -> MemoryPurgeResponse:
    """SPEC §6.5's right to erasure: every fact, every vector, optionally the transcripts.

    The end-user row itself stays. It is what makes an existing request log say who a
    request belonged to, and removing it would rewrite the record of things that happened
    rather than forget what was learned from them.

    ``include_transcripts`` is a query parameter rather than a body: a DELETE with a body
    is legal and unevenly supported — intermediaries drop it — and a flag silently lost in
    transit on an erasure endpoint is the wrong thing to be clever about.
    """
    result = await service.purge(actor, end_user_id, include_transcripts=include_transcripts)
    return MemoryPurgeResponse(facts=result.facts, transcripts=result.transcripts)
