"""``/api/v1/summarization`` — the organization's default model and the ledger's numbers.

The per-connector half of task 102 lives on the connector routes: the mode and caps are a
``summarization`` section of the connector PATCH, and a document's summary is edited and
regenerated under ``/documents/{id}``. What is here is what is *not* about one connector.

**Changing the default model needs ``org:administer``**, like the distillation model: it is
a spending decision.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.control.deps import (
    CurrentActor,
    get_summarization_service,
    require_capability,
)
from app.schemas.summarization import (
    SummarizationHealthResponse,
    SummarizationSettingsRequest,
    SummarizationSettingsResponse,
)
from app.services.monitoring import build_filters
from app.services.permissions import Capability
from app.services.summarization_service import SummarizationService

router = APIRouter(tags=["summarization"])

_Service = Annotated[SummarizationService, Depends(get_summarization_service)]

_reads = Depends(require_capability(Capability.ORG_READ))
_administers = Depends(require_capability(Capability.ORG_ADMINISTER))

_From = Annotated[datetime | None, Query(alias="from")]
_To = Annotated[datetime | None, Query(alias="to")]
_Connector = Annotated[uuid.UUID | None, Query()]


@router.get("/summarization", dependencies=[_reads])
async def get_summarization_settings(
    actor: CurrentActor, service: _Service
) -> SummarizationSettingsResponse:
    """The organization's default summarization model, and what a connector with no choice
    of its own would actually use — through the distillation model and the platform default."""
    return await service.settings(actor)


@router.patch("/summarization", dependencies=[_administers])
async def update_summarization_settings(
    body: SummarizationSettingsRequest, actor: CurrentActor, service: _Service
) -> SummarizationSettingsResponse:
    """``model_id: null`` is a value: it means "summarize with the distillation model"."""
    return await service.update(actor, body.patch())


@router.get("/summarization/health", dependencies=[_reads])
async def get_summarization_health(
    actor: CurrentActor,
    service: _Service,
    start: _From = None,
    to: _To = None,
    connector_id: _Connector = None,
) -> SummarizationHealthResponse:
    """Documents summarized per day, tokens by model, failures, cap hits, the connectors
    spending the most, and the documents parked on a cap — over the monitoring page's own
    window. ``connector_id`` narrows it to one connector's slice for its detail screen."""
    window = build_filters(start=start, end=to)
    return SummarizationHealthResponse.of(
        await service.health(actor, start=window.start, end=window.end, connector_id=connector_id)
    )
