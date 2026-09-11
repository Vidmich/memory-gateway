"""``/api/v1/platform`` — the operator's own screens (SPEC §13.1, §15.3).

Every route here needs ``platform:administer``, which only a superadmin has. That is
unusually blunt for this codebase — most screens split reading from writing — and it is
deliberate: an organization admin has no business knowing what the platform's embedding
model is, how much runway the partitions have, or that another tenant is being deleted
tonight. There is no read-only audience for this router inside a customer.

The one shape worth explaining is the settings PATCH. It looks like an ordinary partial
update and mostly is, except that changing the embedding model cannot be a write — it has
to become a reindex whose *completion* writes the setting, because everything indexed today
agrees with the old model. So the response carries both halves: what was saved, and what is
now in flight. See :mod:`app.services.platform_service`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.control.deps import (
    CurrentActor,
    get_platform_service,
    get_reprocessor,
    require_capability,
)
from app.core.errors import NotFound, Validation
from app.schemas.platform import (
    EffectiveTokenizerResponse,
    ErasureReport,
    MaintenanceResponse,
    MaintenanceRunResponse,
    OrganizationDeletionRequest,
    OrphanGroup,
    PartitionRunway,
    PlatformSettingsPatch,
    PlatformSettingsResponse,
    ReindexEstimate,
    ReindexRequest,
    ReindexRunResponse,
    ReindexTargetResponse,
    RetentionCeilingsResponse,
    SweepRequest,
    SweepResponse,
    VectorBackendsResponse,
    VectorBindingResponse,
    VectorMigrationRequest,
    VectorMigrationResponse,
)
from app.schemas.reprocessing import ReprocessingRunResponse
from app.services.maintenance import Runway, SweepReport
from app.services.maintenance_store import RunState
from app.services.permissions import Capability
from app.services.platform_service import (
    MaintenanceView,
    PlatformService,
    SettingsResult,
    VectorBackendsView,
)
from app.services.reindex import progress_of
from app.services.reindex_store import RunView
from app.services.reprocessing import Reprocessor
from app.services.vector_migration import MigrationPlan

router = APIRouter(prefix="/platform", tags=["platform"])

_Service = Annotated[PlatformService, Depends(get_platform_service)]
_Reprocessor = Annotated[Reprocessor, Depends(get_reprocessor)]

_administers = Depends(require_capability(Capability.PLATFORM_ADMINISTER))

#: The one route in this module an organization may call, and therefore the one that is
#: not under ``/platform``. It answers "why is my retention not what I typed", which is a
#: question the org admin who typed it has to be able to answer without a support ticket.
ceilings_router = APIRouter(tags=["platform"])


@ceilings_router.get(
    "/retention-ceilings",
    dependencies=[Depends(require_capability(Capability.ORG_READ))],
)
async def read_retention_ceilings(service: _Service) -> RetentionCeilingsResponse:
    ceilings = await service.retention_ceilings()
    return RetentionCeilingsResponse(
        max_body_days=ceilings.max_body_days,
        max_metadata_days=ceilings.max_metadata_days,
    )


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


@router.get("/settings", dependencies=[_administers])
async def read_settings(service: _Service) -> PlatformSettingsResponse:
    """Every operator-configurable value, and where each section's value came from.

    Sections listed in ``from_environment`` have no row: they are running on the variable
    that bootstrapped them. That is a different state from "configured to the same value",
    and the screen says which, because the first one changes when a pod is redeployed.
    """
    return _settings_of(await service.read())


@router.patch("/settings", dependencies=[_administers])
async def update_settings(
    actor: CurrentActor, service: _Service, body: PlatformSettingsPatch
) -> PlatformSettingsResponse:
    """Change one or more sections. Audit-logged, at platform scope.

    Changing ``embedding.name`` or ``embedding.dimension`` requires ``confirm_reindex`` to
    repeat the model name, and starts a reindex instead of writing the section — the
    setting lands when the aliases swap.
    """
    return _settings_of(await service.write(actor, body))


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------


@router.get("/maintenance", dependencies=[_administers])
async def read_maintenance(service: _Service) -> MaintenanceResponse:
    """Partition runway, the last run of each scheduled job, and any reindex in flight."""
    return _maintenance_of(await service.maintenance())


@router.post("/maintenance/partitions", dependencies=[_administers])
async def run_partitions(service: _Service) -> MaintenanceRunResponse:
    """Create the missing partitions now.

    Synchronous, unlike everything else an operator can press here, because it is a handful
    of ``CREATE TABLE`` statements and the reason somebody is pressing it is that the runway
    alert has fired — at which point "it is queued" is the wrong answer.
    """
    return _run_of(await service.run_partitions())


@router.post("/maintenance/retention", dependencies=[_administers])
async def run_retention(service: _Service) -> MaintenanceRunResponse:
    """Run the nightly retention pass now, and report what it pruned."""
    return _run_of(await service.run_retention())


@router.post("/maintenance/sweep", dependencies=[_administers])
async def run_sweep(service: _Service, body: SweepRequest) -> SweepResponse:
    """Find vectors and objects with no row behind them.

    ``apply`` defaults to false and nothing is deleted without it. That is not caution
    theatre: the first version of a sweeper is usually wrong in one direction, and the
    wrong direction here destroys customer data that no backup of the database contains.
    """
    return _sweep_of(await service.sweep(apply=body.apply, organization_id=body.organization_id))


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------


@router.post("/reindex", dependencies=[_administers])
async def start_reindex(
    actor: CurrentActor, service: _Service, body: ReindexRequest
) -> ReindexRunResponse | ReindexEstimate:
    """Rebuild collections under the current embedding model.

    ``dry_run`` returns the estimate and starts nothing — which is what the confirmation
    dialog calls, so the cost an operator agrees to is the cost this endpoint counted
    rather than a number the browser worked out for itself.
    """
    if body.dry_run:
        return await service.estimate(body)
    return _reindex_of(await service.reindex(actor, body))


@router.get("/reindex/{run_id}", dependencies=[_administers])
async def read_reindex(
    run_id: uuid.UUID, service: _Service, reprocessor: _Reprocessor
) -> ReindexRunResponse:
    run = await service.reindex_run(run_id)
    if run is None:
        raise NotFound("No such reindex run.")
    response = _reindex_of(run)
    # Task 104: the recut connectors' own runs, so the platform screen can show which
    # connectors this operation is recutting and how far each has got.
    response.reprocessing_runs = [
        ReprocessingRunResponse.of(spawned) for spawned in await reprocessor.spawned_by(run.id)
    ]
    return response


# ---------------------------------------------------------------------------
# vector backends
# ---------------------------------------------------------------------------


@router.get("/vector-backends", dependencies=[_administers])
async def read_vector_backends(service: _Service) -> VectorBackendsResponse:
    """Which backends this deployment can talk to, and where every organization is.

    Read-only, and there is no companion PATCH for the connection details. A backend's
    address is deployment topology and lives in the environment; the one thing here that
    *is* policy — the default for new organizations — is a platform setting.
    """
    return _backends_of(await service.vector_backends())


@router.post(
    "/organizations/{organization_id}/vector-backend",
    dependencies=[_administers],
    status_code=status.HTTP_202_ACCEPTED,
)
async def migrate_vector_backend(
    organization_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    body: VectorMigrationRequest,
) -> VectorMigrationResponse:
    """Move one organization's vectors to another backend.

    Accepted rather than OK: the copy is a job, and the response describes what will be
    moved. Reads keep going to the current backend until the copy is verified and
    promoted, so this endpoint changes nothing a request can observe.
    """
    if body.dry_run:
        return _plan_of(
            await service.plan_vector_migration(organization_id, target=body.backend),
            started=False,
        )
    return _plan_of(
        await service.start_vector_migration(actor, organization_id, target=body.backend),
        started=True,
    )


@router.delete(
    "/organizations/{organization_id}/vector-backend",
    dependencies=[_administers],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_vector_migration(
    organization_id: uuid.UUID, actor: CurrentActor, service: _Service
) -> Response:
    """Abandon a migration in flight and remove what it has built.

    Nothing about reads changes, because nothing about reads ever changed.
    """
    await service.cancel_vector_migration(actor, organization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# erasure
# ---------------------------------------------------------------------------


@router.post(
    "/organizations/{organization_id}/deletion",
    dependencies=[_administers],
    status_code=status.HTTP_202_ACCEPTED,
)
async def schedule_deletion(
    organization_id: uuid.UUID,
    actor: CurrentActor,
    service: _Service,
    body: OrganizationDeletionRequest,
) -> ErasureReport:
    """Mark an organization for deletion. Destroys nothing yet.

    Returns the report shape with nothing removed and ``complete`` false, so the caller
    holds the same artefact before and after — the difference between "requested" and
    "done" is the numbers in it, not two unrelated response bodies.
    """
    purge_after = await service.schedule_deletion(actor, organization_id, body)
    return ErasureReport(
        subject=body.confirm,
        subject_id=organization_id,
        organization_id=organization_id,
        requested_at=purge_after,
        stores=[],
        complete=False,
    )


@router.delete(
    "/organizations/{organization_id}/deletion",
    dependencies=[_administers],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_deletion(
    organization_id: uuid.UUID, actor: CurrentActor, service: _Service
) -> Response:
    """Take it back, while there is still something to take back."""
    await service.cancel_deletion(actor, organization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/organizations/purge", dependencies=[_administers])
async def purge_due(
    service: _Service,
    confirm: Annotated[str, Query()] = "",
) -> list[ErasureReport]:
    """Run the destructive pass for every organization whose grace period has expired.

    Also scheduled nightly; the button exists because "it is deleted" is a promise somebody
    sometimes has to keep on a specific afternoon. ``confirm=purge`` is required, for the
    same reason the sweeper needs ``apply``.
    """
    if confirm != "purge":
        raise Validation("Pass confirm=purge to run the destructive pass.", param="confirm")
    return await service.purge_due()


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _backends_of(view: VectorBackendsView) -> VectorBackendsResponse:
    return VectorBackendsResponse(
        enabled=list(view.enabled),
        default=view.default,
        bindings=[
            VectorBindingResponse(
                organization_id=binding.organization_id,
                backend=binding.backend,
                status=binding.status,
                target=binding.target,
                collection=binding.collection,
            )
            for binding in view.bindings
        ],
    )


def _plan_of(plan: MigrationPlan, *, started: bool) -> VectorMigrationResponse:
    return VectorMigrationResponse(
        organization_id=plan.organization_id,
        source=plan.source,
        target=plan.target,
        target_collection=plan.target_collection,
        points=plan.points,
        facts=plan.facts,
        started=started,
    )


def _settings_of(result: SettingsResult) -> PlatformSettingsResponse:
    return PlatformSettingsResponse(
        settings=result.view.settings,
        attribution=list(result.view.attribution),
        from_environment=list(result.view.from_environment),
        reindex=_reindex_of(result.reindex) if result.reindex is not None else None,
        pending_embedding=result.pending_embedding,
        embedding_tokenizer=EffectiveTokenizerResponse.of(
            result.view.settings.embedding.effective_tokenizer()
        ),
    )


def _maintenance_of(view: MaintenanceView) -> MaintenanceResponse:
    return MaintenanceResponse(
        runway=[_runway_of(entry) for entry in view.runway],
        runway_threshold_days=view.threshold_days,
        last_runs=[_run_of(run) for run in view.runs],
        reindex=_reindex_of(view.reindex) if view.reindex is not None else None,
        recent_reindexes=[_reindex_of(run) for run in view.recent_reindexes],
    )


def _runway_of(entry: Runway) -> PartitionRunway:
    return PartitionRunway(
        table=entry.table,
        days_ahead=entry.days_ahead,
        last_day=entry.last_day.isoformat() if entry.last_day else None,
        low=entry.low,
    )


def _run_of(run: RunState) -> MaintenanceRunResponse:
    return MaintenanceRunResponse(
        id=run.id,
        job=run.job,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        report=dict(run.report),
        error=run.error,
    )


def _reindex_of(run: RunView) -> ReindexRunResponse:
    progress = progress_of(run)
    return ReindexRunResponse(
        id=run.id,
        scope=run.scope,
        organization_id=run.organization_id,
        status=run.status,
        from_model=run.from_model,
        from_dimension=run.from_dimension,
        to_model=run.to_model,
        to_dimension=run.to_dimension,
        estimated_points=run.estimated_points,
        estimated_tokens=run.estimated_tokens,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        targets=[
            ReindexTargetResponse(
                organization_id=target.organization_id,
                collection=target.collection,
                status=target.status,
                total_points=target.total_points,
                done_points=target.done_points,
                started_at=target.started_at,
                finished_at=target.finished_at,
                error=target.error,
            )
            for target in run.targets
        ],
        eta_seconds=progress.eta_seconds,
    )


def _sweep_of(report: SweepReport) -> SweepResponse:
    return SweepResponse(
        applied=report.applied,
        organizations=report.organizations,
        deleted=report.deleted,
        groups=[
            OrphanGroup(
                store=group.store,
                kind=group.kind,
                count=len(group.ids),
                # A handful, not the set: a report is for deciding whether to allow the
                # destructive pass, and a response carrying ten thousand point ids is a
                # data export nobody asked for.
                sample=list(group.ids[:10]),
            )
            for group in report.groups
        ],
    )
