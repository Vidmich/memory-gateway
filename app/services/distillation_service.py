"""The control plane's view of memory write-back (SPEC §6.4, §10.1, §13.1).

Three things an operator does about distillation, and none of them is running it in the
usual sense: read the settings, change them, and look at whether it is working. The fourth —
"Distil now" — is the exception, and it exists because the feature's worst failure mode is
*invisible latency*. Somebody adds a preference, waits thirty seconds, refreshes, sees
nothing, and has no way to tell whether the debounce has not fired, the model is
misconfigured, or the extractor decided the sentence was not durable. One button that runs
the real pass synchronously and reports what it did answers all three.

**The settings are a partial merge, not a replace.** ``organizations.settings`` also holds
the logging defaults and whatever task 17 adds, so this writes one key inside it and leaves
the rest — through :func:`~app.schemas.config.merge_config`, which is strict about unknown
keys for the same reason every other blob in this system is: a stored setting nothing reads
is indistinguishable from a setting that does not work, and the second is what the operator
will conclude.

**The usage figure and the guard read the same table.** ``calls_today`` on the screen is the
count the cap will actually compare against — not a parallel counter that agrees with it
most of the time. A cost guard whose displayed number can drift from its enforced number is
a guard nobody will believe when it fires.

**A missing model is a state, not an error.** An organization that has not chosen one and a
platform with no default produce ``effective_model_id: null`` and a screen that says so.
Distillation then skips with a recorded reason rather than dead-lettering every job, because
"nobody has picked a model" is a configuration step, not an incident.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.core.errors import NotFound
from app.core.tenancy import Actor, TenantScope
from app.schemas.config import merge_config
from app.schemas.distillation import (
    ORG_DISTILLATION,
    DistillationConfig,
    DistillationSettingsResponse,
    DistillationUsage,
    organization_distillation,
)
from app.services.debounce import Debouncer, session_key
from app.services.directory_store import DirectoryStore
from app.services.distillation_models import ModelChoice
from app.services.distillation_store import DistillationStore, MemoryHealth, start_of_day
from app.services.distiller import Distiller, Pass
from app.services.end_user_store import EndUserStore

logger = logging.getLogger(__name__)

#: Days of history the health chart may cover. The upper bound matches the monitoring
#: page's longest window, so the two screens describe the same month.
MAX_HEALTH_DAYS = 90
DEFAULT_HEALTH_DAYS = 30

#: How far back "Distil now" looks for undistilled conversations. Longer than the default
#: body retention, so the button covers everything that could still be read.
MANUAL_WINDOW_DAYS = 45

#: How many of one person's threads a single press will distil. A person with fifty
#: undistilled conversations is a backfill, not a button — and fifty model calls behind one
#: HTTP request is a request that times out.
MAX_MANUAL_SESSIONS = 5

NO_SUCH_END_USER = "No such end user."


class ModelDescriber(Protocol):
    async def describe(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> ModelChoice | None:
        """Name the model that would distil, without decrypting its credential."""
        ...


class SettingsCache(Protocol):
    def forget(self, organization_id: uuid.UUID) -> None: ...


@dataclass(frozen=True, slots=True)
class ManualPass:
    """What one press of "Distil now" did, across however many threads were pending."""

    sessions: int = 0
    inserted: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    rejected: int = 0
    #: The reason from the first pass that did nothing, when nothing was done. What the
    #: screen shows instead of "0 facts", because "the daily cap is spent" and "there was
    #: nothing new to read" are the same number and different problems.
    reason: str | None = None


class DistillationService:
    def __init__(
        self,
        store: DistillationStore,
        *,
        directory: DirectoryStore,
        end_users: EndUserStore,
        distiller: Distiller,
        models: ModelDescriber,
        debouncer: Debouncer,
        cache: SettingsCache | None = None,
    ) -> None:
        self._store = store
        self._directory = directory
        self._end_users = end_users
        self._distiller = distiller
        self._models = models
        self._debouncer = debouncer
        self._cache = cache

    # -- settings ---------------------------------------------------------

    async def settings(self, actor: Actor) -> DistillationSettingsResponse:
        organization_id = actor.scope.require_organization()
        config = organization_distillation(await self._stored(organization_id))
        return await self._view(organization_id, config)

    async def update(self, actor: Actor, patch: dict[str, Any]) -> DistillationSettingsResponse:
        organization_id = actor.scope.require_organization()
        async with self._directory.begin(actor.scope) as transaction:
            organization = await transaction.organization(organization_id)
            if organization is None:  # pragma: no cover - the scope guarantees it
                raise NotFound("No such organization.")
            settings = dict(organization.settings or {})
            settings[ORG_DISTILLATION] = merge_config(
                DistillationConfig,
                settings.get(ORG_DISTILLATION),
                patch,
                field=ORG_DISTILLATION,
            )
            # Reassigned rather than mutated in place: SQLAlchemy tracks JSONB columns by
            # identity, and a nested `dict.__setitem__` on a loaded value is a change it
            # never notices and never writes.
            organization.settings = settings
            await transaction.commit()

        if self._cache is not None:
            # The log flusher holds these for a minute. Dropping the entry means the person
            # who just changed the debounce delay sees it apply to their next request rather
            # than to the one after the cache expires.
            self._cache.forget(organization_id)

        config = DistillationConfig.load(settings[ORG_DISTILLATION])
        logger.info(
            "distillation settings updated",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "audit_action": "organization.distillation.update",
                "changed": sorted(patch),
            },
        )
        return await self._view(organization_id, config)

    # -- health -----------------------------------------------------------

    async def health(self, actor: Actor, *, days: int = DEFAULT_HEALTH_DAYS) -> MemoryHealth:
        window = max(1, min(days, MAX_HEALTH_DAYS))
        end = start_of_day() + timedelta(days=1)
        async with self._store.begin(actor.scope) as transaction:
            return await transaction.health(start=end - timedelta(days=window), end=end)

    # -- distil now -------------------------------------------------------

    async def distil_now(self, actor: Actor, end_user_id: uuid.UUID) -> ManualPass:
        """Run the real pass, synchronously, over whatever this person has pending.

        The *real* pass: the same extractor, the same threshold, the same reconciliation as
        the background job, built from the same function. A button that ran a simplified
        version would answer "is distillation working" with a different question's answer.
        """
        async with self._end_users.begin(actor.scope) as transaction:
            end_user = await transaction.end_user(end_user_id)
            if end_user is None:
                raise NotFound(NO_SUCH_END_USER)
            organization_id = end_user.organization_id

        end = datetime.now(UTC)
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            sessions = list(
                await transaction.pending_sessions(
                    start=end - timedelta(days=MANUAL_WINDOW_DAYS),
                    end=end + timedelta(seconds=1),
                    limit=MAX_MANUAL_SESSIONS,
                    end_user_id=end_user_id,
                )
            )

        result = ManualPass()
        for session in sessions:
            # Whatever the debounce had armed for this thread is now redundant: this pass
            # is about to read its transcripts. Cancelling saves the wasted job rather than
            # letting it wake up and find nothing.
            await self._debouncer.cancel(session_key(end_user_id, session.session_id))
            result = _accumulate(
                result,
                await self._distiller.run(
                    organization_id=organization_id,
                    end_user_id=end_user_id,
                    session_id=session.session_id,
                ),
            )
        logger.info(
            "distilled on demand",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "end_user_id": str(end_user_id),
                "sessions": result.sessions,
                "inserted": result.inserted,
                "audit_action": "end_user.memory.distil",
            },
        )
        return result

    # -- internals --------------------------------------------------------

    async def _stored(self, organization_id: uuid.UUID) -> dict[str, Any]:
        async with self._directory.begin(
            TenantScope.of_organization(organization_id)
        ) as transaction:
            organization = await transaction.organization(organization_id)
            return dict(getattr(organization, "settings", None) or {})

    async def _view(
        self, organization_id: uuid.UUID, config: DistillationConfig
    ) -> DistillationSettingsResponse:
        midnight = start_of_day()
        async with self._store.begin(TenantScope.of_organization(organization_id)) as transaction:
            used = await transaction.calls_since(midnight)
        choice = await self._models.describe(organization_id, config.model_id)
        return DistillationSettingsResponse(
            config=config,
            usage=DistillationUsage(
                calls_today=used,
                daily_call_cap=config.daily_call_cap,
                day_started_at=midnight,
            ),
            effective_model_id=choice.id if choice else None,
            effective_model_name=choice.name if choice else None,
            using_platform_default=bool(choice and choice.from_platform),
        )


def _accumulate(result: ManualPass, outcome: Pass) -> ManualPass:
    return ManualPass(
        sessions=result.sessions + 1,
        inserted=result.inserted + outcome.inserted,
        deduped=result.deduped + outcome.deduped,
        superseded=result.superseded + outcome.superseded,
        evicted=result.evicted + outcome.evicted,
        rejected=result.rejected + outcome.rejected,
        reason=result.reason or (outcome.reason if not outcome.wrote_anything else None),
    )


__all__ = [
    "DEFAULT_HEALTH_DAYS",
    "MANUAL_WINDOW_DAYS",
    "MAX_HEALTH_DAYS",
    "MAX_MANUAL_SESSIONS",
    "NO_SUCH_END_USER",
    "DistillationService",
    "ManualPass",
    "ModelDescriber",
    "SettingsCache",
]
