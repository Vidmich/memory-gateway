"""The validation routes (task 103, SPEC §6.6): index audits and retrieval evaluation.

Two families under one router because they share an audience and a permission model.
Reading a report or a run is ``org:read``; starting one is ``resources:write``, the same
capability that edits the connector or gateway it measures — an audit or a run is a
decision about that resource, and the embedding audit's drift check and the evaluation
run both spend at the embedding provider.

**Nothing here writes to the index.** An audit scrolls it; a run searches it; the drift
check and the question generator call a provider and store what came back beside the
question, never in the collection. That is an acceptance criterion, and the tests assert
the collection is byte-identical before and after.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.control.deps import (
    CurrentActor,
    get_evaluation_service,
    get_index_auditor,
    require_capability,
)
from app.schemas.validation import (
    AuditAlertList,
    AuditAlertResponse,
    AuditRequest,
    AuditResponse,
    AuditStatusResponse,
    EvaluationItemPatch,
    EvaluationItemRequest,
    EvaluationItemResponse,
    EvaluationRunList,
    EvaluationRunResponse,
    EvaluationRunSummaryResponse,
    EvaluationSetDetailResponse,
    EvaluationSetList,
    EvaluationSetPatch,
    EvaluationSetRequest,
    EvaluationSetResponse,
    GenerateRequest,
    GenerateResponse,
    ImportRequest,
    ImportResponse,
    RunDiffResponse,
    RunRequest,
)
from app.services.evaluation_service import EvaluationService, ItemDraft
from app.services.index_auditor import IndexAuditor
from app.services.permissions import Capability

router = APIRouter(tags=["validation"])

_Auditor = Annotated[IndexAuditor, Depends(get_index_auditor)]
_Evaluations = Annotated[EvaluationService, Depends(get_evaluation_service)]

_reads = Depends(require_capability(Capability.ORG_READ))
_writes = Depends(require_capability(Capability.RESOURCES_WRITE))

# ---------------------------------------------------------------------------
# audits
# ---------------------------------------------------------------------------


@router.get("/connectors/{connector_id}/audits", dependencies=[_reads])
async def get_audits(
    connector_id: uuid.UUID, actor: CurrentActor, auditor: _Auditor
) -> AuditStatusResponse:
    """The latest chunking and embedding reports for a connector, their age, and what a
    drift check would cost before it is run."""
    return AuditStatusResponse.of(await auditor.status(actor, connector_id))


@router.post(
    "/connectors/{connector_id}/audits/{kind}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_writes],
)
async def start_audit(
    connector_id: uuid.UUID,
    kind: str,
    actor: CurrentActor,
    auditor: _Auditor,
    body: AuditRequest | None = None,
) -> AuditResponse:
    """Queue an audit. ``kind`` is ``chunking`` or ``embedding``; the body's
    ``drift_sample`` asks the embedding audit to re-embed that many chunks — the one part of
    validation that spends, and the estimate is on the GET. A second request while one is
    running returns the running row."""
    drift = body.drift_sample if body is not None else None
    return AuditResponse.of(await auditor.start(actor, connector_id, kind, drift_sample=drift))


@router.get("/validation/alerts", dependencies=[_reads])
async def list_audit_alerts(actor: CurrentActor, auditor: _Auditor) -> AuditAlertList:
    """Connectors whose latest audit raised a red finding: the dashboard's degraded state."""
    return AuditAlertList(items=[AuditAlertResponse.of(a) for a in await auditor.alerts(actor)])


# ---------------------------------------------------------------------------
# evaluation sets
# ---------------------------------------------------------------------------


@router.get("/gateways/{gateway_id}/evaluation-sets", dependencies=[_reads])
async def list_evaluation_sets(
    gateway_id: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> EvaluationSetList:
    return EvaluationSetList(
        items=[EvaluationSetResponse.of(view) for view in await service.sets(actor, gateway_id)]
    )


@router.post(
    "/gateways/{gateway_id}/evaluation-sets",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_writes],
)
async def create_evaluation_set(
    gateway_id: uuid.UUID, body: EvaluationSetRequest, actor: CurrentActor, service: _Evaluations
) -> EvaluationSetResponse:
    return EvaluationSetResponse.of(
        await service.create_set(actor, gateway_id, name=body.name, description=body.description)
    )


@router.get("/evaluation-sets/{set_id}", dependencies=[_reads])
async def get_evaluation_set(
    set_id: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> EvaluationSetDetailResponse:
    return EvaluationSetDetailResponse.of(await service.detail(actor, set_id))


@router.patch("/evaluation-sets/{set_id}", dependencies=[_writes])
async def update_evaluation_set(
    set_id: uuid.UUID, body: EvaluationSetPatch, actor: CurrentActor, service: _Evaluations
) -> EvaluationSetDetailResponse:
    values = body.model_dump(exclude_unset=True)
    return EvaluationSetDetailResponse.of(
        await service.update_set(
            actor, set_id, name=values.get("name"), description=values.get("description")
        )
    )


@router.delete(
    "/evaluation-sets/{set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_writes],
)
async def delete_evaluation_set(
    set_id: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> Response:
    await service.delete_set(actor, set_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------


@router.post(
    "/evaluation-sets/{set_id}/items",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_writes],
)
async def add_evaluation_item(
    set_id: uuid.UUID, body: EvaluationItemRequest, actor: CurrentActor, service: _Evaluations
) -> EvaluationItemResponse:
    """A labelled question. Labels are checked against this organization's index; a chunk
    that is not there — or is another tenant's — is a 404."""
    draft = ItemDraft(
        question=body.question,
        relevant=[
            {"chunk_id": label.chunk_id, "document_id": str(label.document_id)}
            for label in body.relevant
        ],
        relevant_document_ids=body.relevant_document_ids,
        verified=body.verified,
        notes=body.notes,
    )
    return EvaluationItemResponse.of(await service.add_item(actor, set_id, draft))


@router.patch("/evaluation-items/{item_id}", dependencies=[_writes])
async def update_evaluation_item(
    item_id: uuid.UUID, body: EvaluationItemPatch, actor: CurrentActor, service: _Evaluations
) -> EvaluationItemResponse:
    """Edit the question or the labels, or verify the item — which is what moves it between
    the two columns a run reports."""
    return EvaluationItemResponse.of(await service.update_item(actor, item_id, body.patch()))


@router.delete(
    "/evaluation-items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_writes],
)
async def delete_evaluation_item(
    item_id: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> Response:
    await service.delete_item(actor, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/evaluation-sets/{set_id}/import", dependencies=[_writes])
async def import_evaluation_items(
    set_id: uuid.UUID, body: ImportRequest, actor: CurrentActor, service: _Evaluations
) -> ImportResponse:
    """One item per distinct question the gateway answered in the window, pre-labelled
    with what the answer cited where it cited anything. Everything arrives unverified."""
    return ImportResponse.of(
        await service.import_from_log(
            actor, set_id, start=body.start, end=body.end, uncited=body.uncited, limit=body.limit
        )
    )


@router.post("/evaluation-sets/{set_id}/generate", dependencies=[_writes])
async def generate_evaluation_items(
    set_id: uuid.UUID,
    actor: CurrentActor,
    service: _Evaluations,
    body: GenerateRequest | None = None,
) -> GenerateResponse:
    """Ask a model to write the question each of ``count`` chunks answers. Spends, through
    the summarization model chain, and the items are marked as a model's."""
    count = body.count if body is not None else GenerateRequest().count
    return GenerateResponse.of(await service.generate(actor, set_id, count=count))


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@router.post(
    "/evaluation-sets/{set_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_writes],
)
async def start_evaluation_run(
    set_id: uuid.UUID, actor: CurrentActor, service: _Evaluations, body: RunRequest | None = None
) -> EvaluationRunSummaryResponse:
    """Queue a run through the real retrieval path — one embedding call per question —
    with the saved gateway or the editor's unsaved ``memory_config``."""
    patch = body.memory_config if body is not None else None
    return EvaluationRunSummaryResponse.of(
        await service.start_run(actor, set_id, memory_config=patch)
    )


@router.get("/evaluation-sets/{set_id}/runs", dependencies=[_reads])
async def list_evaluation_runs(
    set_id: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> EvaluationRunList:
    return EvaluationRunList(
        items=[EvaluationRunSummaryResponse.of(run) for run in await service.runs(actor, set_id)]
    )


@router.get("/evaluation-runs/{run_id}", dependencies=[_reads])
async def get_evaluation_run(
    run_id: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> EvaluationRunResponse:
    return EvaluationRunResponse.of(await service.run(actor, run_id))


@router.get("/evaluation-runs/{run_id}/diff/{against}", dependencies=[_reads])
async def diff_evaluation_runs(
    run_id: uuid.UUID, against: uuid.UUID, actor: CurrentActor, service: _Evaluations
) -> RunDiffResponse:
    """Two runs of one set, oldest first: how the numbers moved, which settings differed,
    what the index looked like each time, and which questions flipped."""
    return RunDiffResponse.of(await service.diff(actor, run_id, against))
