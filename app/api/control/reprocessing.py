"""Reprocessing runs (task 104): ``POST /connectors/{id}/reprocess`` and its history.

The successor of ``POST /connectors/{id}/reindex`` (task 20), which stays for one release
as an alias and now starts the same tracked run. The difference a caller sees: this one
returns the run rather than a count, scopes to the stale documents by default, and a
second request while one is going returns the running one with ``created: false``
instead of starting another.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.control.deps import (
    CurrentActor,
    get_connector_service,
    get_reprocessor,
    require_capability,
)
from app.schemas.connector import ConnectorUpdateRequest
from app.schemas.reprocessing import (
    ReprocessingRunList,
    ReprocessingRunResponse,
    ReprocessRequest,
    StaleAlertList,
    StaleAlertResponse,
    StalePreviewResponse,
)
from app.services.connectors import ConnectorService
from app.services.permissions import Capability
from app.services.reprocessing import Reprocessor

router = APIRouter(tags=["reprocessing"])

_Service = Annotated[Reprocessor, Depends(get_reprocessor)]
_Connectors = Annotated[ConnectorService, Depends(get_connector_service)]
_Limit = Annotated[int | None, Query(ge=1, le=100)]

_reads = Depends(require_capability(Capability.ORG_READ))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))


@router.post(
    "/connectors/{connector_id}/reprocess",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_writes],
)
async def reprocess_connector(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    body: ReprocessRequest | None = None,
) -> ReprocessingRunResponse:
    """Start a run over the connector's stale documents (or the scope named), or return
    the one already going. 202 either way: the work is the worker's."""
    request = body or ReprocessRequest()
    started = await service.start(actor, connector_id, scope=request.to_scope())
    return ReprocessingRunResponse.of(started.run, created=started.created)


@router.get("/connectors/{connector_id}/reprocessing-runs", dependencies=[_reads])
async def list_reprocessing_runs(
    connector_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    limit: _Limit = None,
) -> ReprocessingRunList:
    """The connector's history, newest first: trigger, who, when, duration, outcome, and
    the estimate beside what was actually spent."""
    runs = await service.runs(actor, connector_id, limit=limit or 20)
    return ReprocessingRunList(items=[ReprocessingRunResponse.of(run) for run in runs])


@router.get("/reprocessing-runs/{reprocessing_run_id}", dependencies=[_reads])
async def read_reprocessing_run(
    reprocessing_run_id: uuid.UUID, actor: CurrentActor, service: _Service
) -> ReprocessingRunResponse:
    return ReprocessingRunResponse.of(await service.run(actor, reprocessing_run_id))


@router.post(
    "/reprocessing-runs/{reprocessing_run_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_writes],
)
async def retry_failed(
    reprocessing_run_id: uuid.UUID, actor: CurrentActor, service: _Service
) -> ReprocessingRunResponse:
    """**Retry failed**: a new run over exactly the documents this one left failed."""
    started = await service.retry_failed(actor, reprocessing_run_id)
    return ReprocessingRunResponse.of(started.run, created=started.created)


@router.get("/reprocessing/alerts", dependencies=[_reads])
async def list_stale_alerts(
    actor: CurrentActor,
    service: _Service,
    older_than_hours: Annotated[float, Query(ge=0, le=24 * 30)] = 24.0,
) -> StaleAlertList:
    """The dashboard's degraded state: connectors whose documents have been stale for
    longer than the threshold (a day by default), with the age."""
    alerts = await service.alerts(actor, older_than=timedelta(hours=older_than_hours))
    return StaleAlertList(
        items=[
            StaleAlertResponse(
                connector_id=alert.connector_id,
                connector_name=alert.connector_name,
                stale_documents=alert.stale_documents,
                stale_since=alert.stale_since,
                age_hours=alert.age_hours,
            )
            for alert in alerts
        ]
    )


@router.post("/connectors/{connector_id}/stale-preview", dependencies=[_reads])
async def stale_preview(
    connector_id: uuid.UUID,
    body: ConnectorUpdateRequest,
    actor: CurrentActor,
    connectors: _Connectors,
) -> StalePreviewResponse:
    """How many indexed documents saving this patch would mark stale, per format — what
    every configuration form says before its Save button. Writes nothing."""
    formats = await connectors.stale_preview(actor, connector_id, body.to_patch())
    return StalePreviewResponse(formats=formats, total=sum(formats.values()))
