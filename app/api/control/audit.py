"""``/api/v1/audit-events`` — who changed what, and the CSV of it (SPEC §10.4).

Two reads, gated on ``org:read``. That is the one access decision here worth arguing
about, and it lands the same way monitoring's did: SPEC §5.2 makes ``org_viewer``
read-only across the organization, and the audit log is strictly *less* sensitive than
the request detail view an ``org_viewer`` can already open — one holds configuration
changes, the other holds end-user prompt bodies. Gating it higher would mean the person
answering "who turned this off yesterday" needs the permission to turn it back on.

Filtering is org-scoped by construction: the scope clause is in the repository, so
``organization_id`` here can only ever *narrow*. For a platform administrator that is how
one customer is picked out of the whole log; for anybody else, an id that is not theirs
returns nothing rather than something.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.control.deps import CurrentActor, get_audit_service, require_capability
from app.schemas.audit import AuditEventResponse
from app.schemas.common import Page
from app.services.audit_service import AuditService, build_audit_filters
from app.services.permissions import Capability

router = APIRouter(tags=["audit"])

_Service = Annotated[AuditService, Depends(get_audit_service)]
_reads = Depends(require_capability(Capability.ORG_READ))

# `from` is a Python keyword, so the parameter is `start` and the wire name is the alias —
# the same arrangement as the monitoring routes, and the same names SPEC §12.2 uses.
_From = Annotated[datetime | None, Query(alias="from")]
_To = Annotated[datetime | None, Query(alias="to")]
_Organization = Annotated[uuid.UUID | None, Query()]
_Actor = Annotated[uuid.UUID | None, Query()]
_Action = Annotated[str | None, Query(max_length=64)]
_TargetType = Annotated[str | None, Query(max_length=32)]
_TargetId = Annotated[uuid.UUID | None, Query()]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]


@router.get("/audit-events", dependencies=[_reads])
async def list_audit_events(
    actor: CurrentActor,
    service: _Service,
    organization_id: _Organization = None,
    actor_user_id: _Actor = None,
    action: _Action = None,
    target_type: _TargetType = None,
    target_id: _TargetId = None,
    start: _From = None,
    to: _To = None,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[AuditEventResponse]:
    """Newest first, cursor-paginated.

    ``target_type`` plus ``target_id`` is the contextual history a gateway, model or
    connector screen asks for — which is where this log is actually read, rather than on
    the full-list screen somebody opens once a quarter.
    """
    filters = build_audit_filters(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        start=start,
        end=to,
    )
    page = await service.list_events(actor, filters, cursor=cursor, limit=limit)
    return Page(
        items=[AuditEventResponse.of(event) for event in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/audit-events/export", dependencies=[_reads])
async def export_audit_events(
    actor: CurrentActor,
    service: _Service,
    organization_id: _Organization = None,
    actor_user_id: _Actor = None,
    action: _Action = None,
    target_type: _TargetType = None,
    target_id: _TargetId = None,
    start: _From = None,
    to: _To = None,
) -> StreamingResponse:
    """The same filter, as a CSV file, streamed.

    No cursor and no limit: an export is the whole result, up to the service's row cap.
    ``Content-Disposition`` names a file rather than letting the browser render the text,
    because the one thing somebody does with this response is open it in a spreadsheet.
    """
    filters = build_audit_filters(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        start=start,
        end=to,
    )
    return StreamingResponse(
        await service.export(actor, filters),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": 'attachment; filename="audit-events.csv"'},
    )


__all__ = ["router"]
