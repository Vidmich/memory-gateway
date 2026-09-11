"""What the Platform screens call: one object over settings, jobs, reindex and erasure.

A facade rather than four dependencies on the router, for one reason that is worth the
extra file: **changing the embedding model is not a settings write**. It looks like one on
the screen and it arrives as one on the wire, and underneath it has to become a reindex
whose completion writes the setting. Somebody has to make that translation, and a route
handler is the wrong place — it would put the product's most expensive decision in the
layer that is meant to be about HTTP.

So this module owns the rule: a PATCH is split into the sections that can simply be
written and the one that cannot, the second becomes a run, and the response says both what
changed and what is now in flight. Everything else here is thin.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.errors import Validation
from app.core.tenancy import Actor
from app.schemas.platform import (
    EmbeddingChoice,
    ErasureReport,
    OrganizationDeletionRequest,
    PlatformSettingsPatch,
    ReindexEstimate,
    ReindexRequest,
    RetentionCeilings,
)
from app.services.audit import Attribution, Target
from app.services.erasure import OrganizationEraser
from app.services.jobs import (
    RECONCILE_INDEX,
    REINDEX,
    JobQueue,
    JobRequest,
    reconcile_key,
    reindex_key,
)
from app.services.maintenance import (
    LOW_RUNWAY_DAYS,
    PARTITIONS,
    RETENTION,
    SWEEP,
    OrphanSweeper,
    PartitionManager,
    RetentionJob,
    Runway,
    SweepReport,
)
from app.services.maintenance_store import MaintenanceStore, RunState
from app.services.platform_settings import PlatformSettingsService, PlatformView
from app.services.reindex import Reindexer
from app.services.reindex_store import RunView
from app.services.vector_backends import VectorBackends
from app.services.vector_binding_store import Binding
from app.services.vector_migration import MigrationPlan, VectorMigrator

logger = logging.getLogger(__name__)

#: The job name the worker registers for a reindex run. Re-exported from
#: :mod:`app.services.jobs` so a screen that talks about it does not have to import the
#: queue module to name it.
REINDEX_JOB = REINDEX


#: Audit actions for task 19. Two, not one: an operator reading the log needs "somebody
#: moved a tenant" and "somebody changed their mind" to be different lines.
VECTOR_BACKEND_MIGRATED = "vector_backend.migrate"
VECTOR_MIGRATION_CANCELLED = "vector_backend.cancel"


@dataclass(frozen=True, slots=True)
class VectorBackendsView:
    """What the platform offers and where everybody is."""

    enabled: Sequence[str]
    default: str
    bindings: Sequence[Binding]


@dataclass(frozen=True, slots=True)
class SettingsResult:
    """A settings read or write, plus whatever it set in motion."""

    view: PlatformView
    #: The run a PATCH started, when it changed the embedding model. ``None`` otherwise.
    reindex: RunView | None = None
    #: The model being moved to while ``reindex`` is running. Read off the run rather than
    #: the settings, because the settings deliberately still say the old one — see
    #: :mod:`app.services.reindex`.
    pending_embedding: EmbeddingChoice | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceView:
    runway: tuple[Runway, ...]
    threshold_days: int
    runs: tuple[RunState, ...]
    reindex: RunView | None
    recent_reindexes: tuple[RunView, ...]


class PlatformService:
    """Superadmin operations, in one place."""

    def __init__(
        self,
        *,
        settings: PlatformSettingsService,
        partitions: PartitionManager,
        retention: RetentionJob,
        sweeper: OrphanSweeper,
        reindexer: Reindexer,
        eraser: OrganizationEraser,
        store: MaintenanceStore,
        backends: VectorBackends | None = None,
        migrator: VectorMigrator | None = None,
        queue: JobQueue | None = None,
    ) -> None:
        self._settings = settings
        self._partitions = partitions
        self._retention = retention
        self._sweeper = sweeper
        self._reindexer = reindexer
        self._eraser = eraser
        self._store = store
        self._backends = backends
        self._migrator = migrator
        self._queue = queue

    # -- vector backends -------------------------------------------------

    async def vector_backends(self) -> VectorBackendsView:
        """What this deployment offers, and where every organization currently is.

        The enabled set comes from the environment and is not editable here — a vector
        backend's address is deployment topology, and a settings screen that could point
        one somewhere new would be the tenant-adjacent-URL problem one level up (see
        :mod:`app.services.vector_backends`). What *is* editable is the default for new
        organizations, which is policy.
        """
        registry = self._require_backends()
        return VectorBackendsView(
            enabled=registry.enabled(),
            default=registry.default,
            bindings=await registry.bindings.all(),
        )

    async def plan_vector_migration(
        self, organization_id: uuid.UUID, *, target: str
    ) -> MigrationPlan:
        return await self._require_migrator().plan(organization_id, target=target)

    async def start_vector_migration(
        self, actor: Actor, organization_id: uuid.UUID, *, target: str
    ) -> MigrationPlan:
        plan = await self._require_migrator().start(organization_id, target=target)
        await self._record(
            actor,
            VECTOR_BACKEND_MIGRATED,
            organization_id=organization_id,
            summary={"from": plan.source, "to": plan.target, "points": plan.points},
        )
        return plan

    async def cancel_vector_migration(self, actor: Actor, organization_id: uuid.UUID) -> None:
        await self._require_migrator().cancel(organization_id)
        await self._record(
            actor,
            VECTOR_MIGRATION_CANCELLED,
            organization_id=organization_id,
            summary={},
        )

    def _require_backends(self) -> VectorBackends:
        if self._backends is None:  # pragma: no cover - wiring error, not a request
            raise RuntimeError("this platform service was built without a vector registry")
        return self._backends

    def _require_migrator(self) -> VectorMigrator:
        if self._migrator is None:  # pragma: no cover - wiring error, not a request
            raise RuntimeError("this platform service was built without a migrator")
        return self._migrator

    async def _record(
        self,
        actor: Actor,
        action: str,
        *,
        organization_id: uuid.UUID,
        summary: Mapping[str, Any],
    ) -> None:
        """One audit event, into the *platform's* log.

        Not the customer's, for the reason task 15 already established for every other
        operator action: a platform-wide decision recorded inside one tenant is both wrong
        and, for every other tenant, invisible.
        """
        async with self._store.begin() as transaction:
            transaction.audit(
                Attribution.of(actor),
                action,
                target=Target(type="organization", id=organization_id, label=str(organization_id)),
                organization_id=None,
                summary=dict(summary),
            )
            await transaction.commit()

    # -- settings --------------------------------------------------------

    async def read(self) -> SettingsResult:
        view = await self._settings.view()
        running = await self._reindexer.running()
        return SettingsResult(
            view=view,
            reindex=running,
            pending_embedding=_pending(running, view),
        )

    async def write(self, actor: Actor, patch: PlatformSettingsPatch) -> SettingsResult:
        """Apply the sections that are writes, and turn an embedding change into a run.

        The provider is the exception inside the exception. Moving the *same* model to a
        different endpoint produces the same vectors, so it is an ordinary write even
        though it arrives in the embedding section — and forcing a re-embed of every
        corpus to change a base URL would be an absurd price for a routing change.
        """
        current = (await self._settings.current()).embedding
        sections = patch.sections()
        embedding = sections.pop("embedding", None)
        run: RunView | None = None

        if embedding is not None:
            proposed = EmbeddingChoice.model_validate({**current.model_dump(), **embedding})
            if current.same_as(proposed):
                sections["embedding"] = embedding
            else:
                if patch.confirm_reindex != proposed.name:
                    raise Validation(
                        "Changing the embedding model re-embeds every collection. "
                        f"Repeat the model name in 'confirm_reindex' to proceed: "
                        f"{proposed.name}.",
                        param="confirm_reindex",
                    )
                run = await self._reindexer.start(actor, choice=proposed)
                await self._enqueue(run)
                # The halves of the change that need no reindex still land now, so a
                # move that is both a new endpoint and a new model does not leave the
                # endpoint waiting behind the re-embed. The tokenizer override (task 101)
                # is one of them: it invalidates chunks through the fingerprint, which the
                # connectors report on their own terms, not through this run.
                now = {
                    key: value
                    for key, value in embedding.items()
                    if key in ("provider", "tokenizer")
                }
                if now:
                    sections["embedding"] = now

        tokenizer_moved = "tokenizer" in (sections.get("embedding") or {})
        view = (
            await self._settings.update(actor, PlatformSettingsPatch.model_validate(sections))
            if sections
            else await self._settings.view()
        )
        if tokenizer_moved and self._queue is not None:
            # Task 104. A tokenizer change invalidates every chunk everywhere without a
            # connector save to mark them, so the marking is a job: every connector's
            # stored index status recomputed from its fingerprints.
            await self._queue.enqueue(
                JobRequest(
                    name=RECONCILE_INDEX,
                    payload={"reason": "tokenizer"},
                    idempotency_key=reconcile_key("tokenizer"),
                )
            )
        return SettingsResult(view=view, reindex=run, pending_embedding=_pending(run, view))

    async def retention_ceilings(self) -> RetentionCeilings:
        """The retention maxima alone, for a screen inside an organization."""
        return (await self._settings.current()).retention

    # -- maintenance -----------------------------------------------------

    async def maintenance(self) -> MaintenanceView:
        runway = await self._partitions.runway()
        async with self._store.begin() as transaction:
            runs = await transaction.recent_runs(limit=20)
        recent = await self._reindexer.recent(limit=10)
        return MaintenanceView(
            runway=tuple(runway),
            threshold_days=runway[0].threshold if runway else LOW_RUNWAY_DAYS,
            runs=tuple(_newest_per_job(runs)),
            reindex=next((run for run in recent if run.status == "running"), None),
            recent_reindexes=tuple(recent),
        )

    async def run_partitions(self) -> RunState:
        report = await self._partitions.ensure()
        async with self._store.begin() as transaction:
            run = await transaction.open_run(PARTITIONS)
            await transaction.save_run(run.id, report=report.as_json(), status="succeeded")
            await transaction.commit()
            found = await transaction.recent_runs(limit=1)
        return found[0] if found else run

    async def run_retention(self) -> RunState:
        config = await self._settings.current()
        await self._retention.configured_with(config).run()
        async with self._store.begin() as transaction:
            recent = await transaction.recent_runs(limit=20)
        return next(run for run in recent if run.job == RETENTION)

    async def sweep(
        self, *, apply: bool = False, organization_id: uuid.UUID | None = None
    ) -> SweepReport:
        return await self._sweeper.sweep(apply=apply, organization_id=organization_id)

    # -- reindex ---------------------------------------------------------

    async def estimate(self, request: ReindexRequest) -> ReindexEstimate:
        return await self._reindexer.estimate(organization_id=request.organization_id)

    async def reindex(self, actor: Actor, request: ReindexRequest) -> RunView:
        run = await self._reindexer.start(actor, organization_id=request.organization_id)
        await self._enqueue(run)
        return run

    async def reindex_run(self, run_id: uuid.UUID) -> RunView | None:
        return await self._reindexer.find(run_id)

    async def _enqueue(self, run: RunView) -> None:
        """Hand the run to the worker.

        A failure here is logged, not raised: the run row exists and says ``running``, and
        an operator pressing the button again gets a 409 naming it rather than a duplicate.
        Failing the request would leave a row nobody can act on and no way to see why.
        """
        if self._queue is None:
            return
        try:
            await self._queue.enqueue(
                JobRequest(
                    name=REINDEX_JOB,
                    payload={"run_id": str(run.id)},
                    idempotency_key=reindex_key(run.id),
                )
            )
        except Exception:
            logger.error(
                "could not enqueue the reindex; it is recorded and can be restarted",
                extra={"run_id": str(run.id)},
                exc_info=True,
            )

    # -- erasure ---------------------------------------------------------

    async def schedule_deletion(
        self, actor: Actor, organization_id: uuid.UUID, request: OrganizationDeletionRequest
    ) -> datetime:
        return await self._eraser.request(
            actor,
            organization_id,
            confirm=request.confirm,
            grace_days=request.grace_days,
        )

    async def cancel_deletion(self, actor: Actor, organization_id: uuid.UUID) -> None:
        await self._eraser.cancel(actor, organization_id)

    async def purge_due(self) -> list[ErasureReport]:
        """The scheduled half: every organization whose grace period has run out."""
        return [await self._eraser.purge(identifier) for identifier in await self._eraser.due()]

    # -- internals -------------------------------------------------------


def _pending(run: RunView | None, view: PlatformView) -> EmbeddingChoice | None:
    if run is None or run.status != "running":
        return None
    if view.settings.embedding.name == run.to_model:
        return None
    return EmbeddingChoice(
        provider=view.settings.embedding.provider,
        name=run.to_model,
        dimension=run.to_dimension,
    )


def _newest_per_job(runs: list[RunState]) -> list[RunState]:
    """The most recent run of each job, in the order the jobs are listed.

    The Maintenance screen answers "when did each of these last run", and a plain list of
    the twenty newest rows would show three retention runs and no sweep at all on a busy
    night.
    """
    seen: dict[str, RunState] = {}
    for run in runs:
        seen.setdefault(run.job, run)
    return [seen[job] for job in (PARTITIONS, RETENTION, SWEEP) if job in seen]


__all__ = [
    "REINDEX_JOB",
    "MaintenanceView",
    "PlatformService",
    "SettingsResult",
]
