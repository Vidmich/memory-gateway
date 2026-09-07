"""``/api/v1/metrics`` and ``/api/v1/logs`` — the monitoring screen's data.

Every endpoint here is a read gated on ``org:read``, which every role has. That is
deliberate and it is the one access decision in this file worth arguing about: request
bodies contain end-user content, and an ``org_viewer`` can read them. The alternative —
gating the detail view on ``resources:write`` — would mean the person triaging a support
ticket needs the permission to reconfigure production in order to answer it, which is a
worse security posture than the one it replaces. The control an organization actually has
is the logging configuration: if bodies should not be readable, they should not be stored,
and that switch is on the gateway's Logging section.

The filters arrive as query parameters and are validated in
:func:`app.services.monitoring.build_filters` rather than by FastAPI types alone, because
the interesting rules are relational — ``to`` after ``from``, the window inside a maximum
— and a per-field constraint cannot express them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.control.deps import CurrentActor, get_monitoring_service, require_capability
from app.schemas.common import Page
from app.schemas.monitoring import (
    RequestDetailResponse,
    RequestLogResponse,
    SeriesResponse,
    SummaryResponse,
)
from app.services.monitoring import MonitoringService, build_filters, check_metric
from app.services.permissions import Capability

router = APIRouter(tags=["monitoring"])

_Service = Annotated[MonitoringService, Depends(get_monitoring_service)]
_reads = Depends(require_capability(Capability.ORG_READ))

# `from` is a Python keyword, so the parameter is `start` and its wire name is set by the
# alias. The wire name is what SPEC §12.2 specifies and what a person types into a URL.
_From = Annotated[datetime | None, Query(alias="from")]
_To = Annotated[datetime | None, Query(alias="to")]
_Gateway = Annotated[uuid.UUID | None, Query()]
_Model = Annotated[uuid.UUID | None, Query()]
_StatusClass = Annotated[str | None, Query(max_length=3)]
_EndUser = Annotated[uuid.UUID | None, Query()]
_Session = Annotated[str | None, Query(max_length=128)]
_Streamed = Annotated[bool | None, Query()]
_MinLatency = Annotated[int | None, Query(ge=0)]
_Search = Annotated[str | None, Query(max_length=200)]
_Cursor = Annotated[str | None, Query(max_length=64)]
_Limit = Annotated[int | None, Query(ge=1, le=200)]


@router.get("/metrics/summary", dependencies=[_reads])
async def metrics_summary(
    actor: CurrentActor,
    service: _Service,
    start: _From = None,
    to: _To = None,
    gateway_id: _Gateway = None,
    upstream_model_id: _Model = None,
    status_class: _StatusClass = None,
    end_user_id: _EndUser = None,
) -> SummaryResponse:
    """Totals, percentiles, per-model traffic and the error taxonomy, in one read.

    Cached for 30 seconds per organization and query. Everything on the dashboard and the
    top of the monitoring screen comes from here, so it is the one query worth caching.
    """
    filters = build_filters(
        start=start,
        end=to,
        gateway_id=gateway_id,
        upstream_model_id=upstream_model_id,
        status_class=status_class,
        end_user_id=end_user_id,
    )
    return SummaryResponse.of(await service.summary(actor, filters))


@router.get("/metrics/timeseries", dependencies=[_reads])
async def metrics_timeseries(
    actor: CurrentActor,
    service: _Service,
    start: _From = None,
    to: _To = None,
    metric: Annotated[str, Query()] = "requests",
    group_by: Annotated[str, Query()] = "none",
    interval: Annotated[int | None, Query(ge=1)] = None,
    gateway_id: _Gateway = None,
    upstream_model_id: _Model = None,
    status_class: _StatusClass = None,
    end_user_id: _EndUser = None,
) -> SeriesResponse:
    """Bucketed series. The bucket width is the server's decision, not the client's.

    A metric is usually several lines — ``latency`` is p50/p95/p99 plus TTFT and
    retrieval — so one call fills one chart rather than one line. ``interval`` is a
    request, not an instruction: the response says what was used.
    """
    chosen, grouping = check_metric(metric, group_by)
    filters = build_filters(
        start=start,
        end=to,
        gateway_id=gateway_id,
        upstream_model_id=upstream_model_id,
        status_class=status_class,
        end_user_id=end_user_id,
    )
    series = await service.timeseries(
        actor, filters, metric=chosen, group_by=grouping, interval_seconds=interval
    )
    return SeriesResponse.of(series)


@router.get("/logs", dependencies=[_reads])
async def list_logs(
    actor: CurrentActor,
    service: _Service,
    start: _From = None,
    to: _To = None,
    gateway_id: _Gateway = None,
    upstream_model_id: _Model = None,
    status_class: _StatusClass = None,
    end_user_id: _EndUser = None,
    session_id: _Session = None,
    streamed: _Streamed = None,
    min_latency_ms: _MinLatency = None,
    search: _Search = None,
    cursor: _Cursor = None,
    limit: _Limit = None,
) -> Page[RequestLogResponse]:
    """The request table: metadata only, newest first, cursor-paginated.

    Cursor rather than offset because this list is being written to while it is read —
    SPEC §12.2's reason, and the one screen where an offset page would visibly duplicate
    and skip rows as traffic arrives.
    """
    filters = build_filters(
        start=start,
        end=to,
        gateway_id=gateway_id,
        upstream_model_id=upstream_model_id,
        status_class=status_class,
        end_user_id=end_user_id,
        session_id=session_id,
        streamed=streamed,
        min_latency_ms=min_latency_ms,
        search=search,
    )
    page = await service.list_logs(actor, filters, cursor=cursor, limit=limit)
    return Page(
        items=[RequestLogResponse.of(row) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/logs/{log_id}", dependencies=[_reads])
async def get_log(
    log_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
) -> RequestDetailResponse:
    """One request in full, including whatever of the transcript was stored.

    No time range: the id is a UUIDv7 and carries the millisecond it was minted, so the
    server works out which day's partition to look in. A 404 covers "no such request" and
    "another organization's request" alike.
    """
    return RequestDetailResponse.of(await service.get_log(actor, log_id))


__all__ = ["router"]
