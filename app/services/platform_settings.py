"""Platform configuration: environment first, database second, cache in front.

SPEC §15.3 makes every value an environment variable validated at startup. Task 17 keeps
that and adds a layer above it, and the precedence is the whole design:

1. the environment variable is the **bootstrap** — what a fresh database runs on, and what
   a deployment with no rows configured still behaves like;
2. a row in ``platform_settings`` **overrides** it, per section;
3. deleting the row is not a thing the API offers, because "revert to the environment"
   would mean an operator's screen showing a value that changes when a pod is redeployed.

Bootstrapping rather than seeding matters more than it looks. Writing the environment into
the database at migration time would freeze whatever the machine running the migration
happened to have configured — usually a CI container with ``EMBEDDING_PROVIDER=hash`` —
and the first symptom would be a production deployment quietly embedding with the local
lexical model. Reading the environment on every resolution instead means an unconfigured
platform tracks its own configuration, and a configured one ignores it.

**The cache is a snapshot, not a lookup.** Two of these values are read on the request
path: the rate-limit ceilings on every proxied request, and the storage caps on every
upload. Both are read *synchronously*, in code that has no session to spare, so
:attr:`PlatformSettingsService.snapshot` is a plain attribute holding the last resolved
value and a background refresh keeps it current. A write invalidates it immediately in the
process that made the change — the operator who just pressed Save must not see the old
number — and other replicas pick it up within one refresh interval. That bound is stated
rather than hidden: a pub/sub channel would close it, at the cost of a second thing that
has to be running for configuration to be correct, and these are settings that change a
few times a year.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import Validation
from app.core.tenancy import Actor
from app.schemas.config import merge_config
from app.schemas.gateway_config import Quota
from app.schemas.platform import (
    SECTIONS,
    PlatformSettings,
    PlatformSettingsPatch,
    RetentionCeilings,
    SettingAttribution,
)
from app.services.audit import Attribution, Target
from app.services.limits import Ceilings
from app.services.platform_store import PlatformSettingsStore, StoredSetting, as_json

logger = logging.getLogger(__name__)

#: How often a replica re-reads the sections. Short enough that an operator watching a
#: second replica sees their change without wondering, long enough that it is one query
#: every half-minute per process rather than a load-bearing hot path.
REFRESH_SECONDS = 30.0

#: The audit action for a section change. One action with the section in the target, not
#: six actions: a filter for "platform settings changed" is the question people ask, and
#: splitting it by section would make that filter six checkboxes.
SETTINGS_UPDATED = "platform_settings.update"


def bootstrap(settings: Settings) -> dict[str, Any]:
    """The sections as the environment configures them.

    Every value here has a home in :class:`~app.core.config.Settings`, which is what makes
    a deployment with an empty ``platform_settings`` table behave exactly as it did before
    this task existed. Sections with nothing in the environment are absent rather than
    empty, so the schema's own defaults apply.
    """
    return {
        "embedding": {
            "provider": settings.embedding_provider,
            "name": settings.embedding_model,
            "dimension": settings.embedding_dimension,
        },
        "distillation": {
            "model_id": str(settings.distillation_model_id)
            if settings.distillation_model_id
            else None
        },
        "limits": {
            "global_model_ceilings": {
                "requests_per_minute": settings.global_model_requests_per_minute,
                "tokens_per_minute": settings.global_model_tokens_per_minute,
                "requests_per_day": settings.global_model_requests_per_day,
                "concurrent_requests": settings.global_model_concurrent_requests,
            }
        },
        "storage": {
            "max_file_bytes": settings.upload_max_file_bytes,
            "quota_bytes": settings.storage_quota_bytes,
        },
    }


def resolve(settings: Settings, rows: Sequence[StoredSetting]) -> PlatformSettings:
    """Bootstrap overlaid with whatever is stored.

    Per section, not per field: a stored ``embedding`` row replaces the environment's
    embedding wholesale. That is the same atomicity the section grouping exists for — a
    model from the database and a dimension from the environment is precisely the mismatch
    :class:`~app.schemas.platform.EmbeddingChoice` was made one object to prevent.

    A row whose value no longer validates is *dropped*, loudly, rather than raising. This
    function runs during startup and on the request path; a settings row written by a
    newer build and rolled back must not stop the platform serving traffic.
    """
    merged = dict(bootstrap(settings))
    for row in rows:
        if row.key not in SECTIONS or not isinstance(row.value, Mapping):
            logger.warning("ignoring unknown platform setting", extra={"key": row.key})
            continue
        merged[row.key] = dict(row.value)
    try:
        return PlatformSettings.model_validate(merged)
    except Exception:
        logger.exception("stored platform settings do not validate; using the environment")
        return PlatformSettings.model_validate(bootstrap(settings))


def ceilings_of(config: PlatformSettings) -> Ceilings:
    """The four global-model maxima, as the limiter's own type.

    A function rather than ``Ceilings.of(config)``: that classmethod reads attributes named
    ``global_model_*`` off a duck-typed object, and giving :class:`PlatformSettings` four
    aliases with those names purely to satisfy it would be a worse kind of coupling than a
    three-line translation.
    """
    quota: Quota = config.limits.global_model_ceilings
    return Ceilings(
        requests_per_minute=quota.requests_per_minute,
        requests_per_day=quota.requests_per_day,
        tokens_per_minute=quota.tokens_per_minute,
        concurrent_requests=quota.concurrent_requests,
    )


@dataclass(frozen=True, slots=True)
class PlatformView:
    """What the settings screen renders: the values, and where each one came from."""

    settings: PlatformSettings
    attribution: tuple[SettingAttribution, ...] = ()
    #: Sections with no row, still running on their environment bootstrap.
    from_environment: tuple[str, ...] = ()


class PlatformSettingsService:
    """The resolved configuration, cached, and the one way to change it."""

    def __init__(
        self,
        store: PlatformSettingsStore,
        *,
        settings: Settings,
        refresh_seconds: float = REFRESH_SECONDS,
        catalog_check: Callable[[uuid.UUID], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._refresh_seconds = refresh_seconds
        #: Resolved from the environment alone until the first read lands. Never ``None``,
        #: so nothing on the request path has to handle "settings not loaded yet" — the
        #: worst case is one interval of pre-task-17 behaviour, which is a defined state.
        self._snapshot = resolve(settings, ())
        self._loaded = False
        self._task: asyncio.Task[None] | None = None
        #: Injected so the service can refuse a distillation model that is not a global
        #: catalog entry without importing the catalog and its secret box.
        self._catalog_check = catalog_check

    # -- reading ---------------------------------------------------------

    @property
    def snapshot(self) -> PlatformSettings:
        """The last resolved value, synchronously. See the module docstring."""
        return self._snapshot

    @property
    def loaded(self) -> bool:
        """False until a database read has succeeded, so a caller that needs certainty —
        a reindex deciding what to embed with — can wait rather than act on the bootstrap."""
        return self._loaded

    async def current(self) -> PlatformSettings:
        if not self._loaded:
            await self.refresh()
        return self._snapshot

    async def warm(self) -> PlatformSettings:
        """Load once at startup, and carry on if the database is not there yet.

        A process that cannot read its settings still has to serve: it runs on the
        environment bootstrap, which is exactly what it ran on before this table existed,
        and the background refresh picks the rows up the moment they are readable. Refusing
        to start would turn a slow database into an outage of every replica at once.
        """
        try:
            return await self.refresh()
        except Exception:
            logger.warning(
                "could not read platform settings at startup; using the environment",
                exc_info=True,
            )
            return self._snapshot

    async def refresh(self) -> PlatformSettings:
        async with self._store.begin() as transaction:
            rows = await transaction.all()
        self._snapshot = resolve(self._settings, rows)
        self._loaded = True
        return self._snapshot

    async def view(self) -> PlatformView:
        async with self._store.begin() as transaction:
            rows = await transaction.all()
        settings = resolve(self._settings, rows)
        self._snapshot = settings
        self._loaded = True
        stored = {row.key for row in rows if row.key in SECTIONS}
        return PlatformView(
            settings=settings,
            attribution=tuple(
                SettingAttribution(
                    key=row.key,
                    updated_at=row.updated_at,
                    updated_by=row.updated_by,
                    updated_by_label=row.label,
                )
                for row in rows
                if row.key in SECTIONS
            ),
            from_environment=tuple(name for name in SECTIONS if name not in stored),
        )

    # -- writing ---------------------------------------------------------

    async def update(
        self, actor: Actor | Attribution, patch: PlatformSettingsPatch
    ) -> PlatformView:
        """Apply a partial update, one row per section touched.

        Validation goes through :func:`~app.schemas.config.merge_config`, which refuses a
        key the section does not define. That is what stops ``{"dimenson": 1536}`` from
        being stored, ignored, and discovered when somebody notices retrieval never
        changed — the same rule the gateway configuration blobs follow, for the same
        reason.
        """
        sections = patch.sections()
        if not sections:
            raise Validation("Nothing to change.", param="settings")

        async with self._store.begin() as transaction:
            rows = await transaction.all()
            before = resolve(self._settings, rows)
            stored = {row.key: row.value for row in rows}

            merged: dict[str, Any] = {}
            for name, value in sections.items():
                current = stored.get(name)
                merged[name] = merge_config(
                    _section_schema(name),
                    current
                    if isinstance(current, Mapping)
                    else _bootstrap_section(self._settings, name),
                    value,
                    field=name,
                )

            after = resolve(
                self._settings,
                [
                    *(row for row in rows if row.key not in merged),
                    *(
                        StoredSetting(key=name, value=value, updated_at=datetime.now(UTC))
                        for name, value in merged.items()
                    ),
                ],
            )
            await self._check(before, after, patch)

            user_id = actor.user_id if isinstance(actor, Actor) else None
            for name, value in merged.items():
                await transaction.put(name, as_json(value), updated_by=user_id)
            transaction.audit(
                actor,
                SETTINGS_UPDATED,
                target=_target(sorted(merged)),
                organization_id=None,
                before=_snapshot(before, merged),
                after=_snapshot(after, merged),
            )
            await transaction.commit()

        self._snapshot = after
        self._loaded = True
        return await self.view()

    async def _check(
        self, before: PlatformSettings, after: PlatformSettings, patch: PlatformSettingsPatch
    ) -> None:
        """The two rules that cannot live in the schema.

        A schema validates a value against itself. These validate it against something
        else — the catalog, and the change the operator is actually making — which is why
        they are here and asynchronous.
        """
        # SPEC §9.4: changing the model means re-embedding every collection. The
        # confirmation is the model *name*, retyped, not a boolean: a PATCH carrying
        # `{"confirm": true}` is one somebody could send by copying an example, and this
        # one costs a corpus of embeddings.
        if (
            not before.embedding.same_as(after.embedding)
            and patch.confirm_reindex != after.embedding.name
        ):
            raise Validation(
                "Changing the embedding model re-embeds every collection. "
                f"Repeat the model name in 'confirm_reindex' to proceed: "
                f"{after.embedding.name}.",
                param="confirm_reindex",
            )

        model_id = after.distillation.model_id
        if (
            model_id is not None
            and self._catalog_check is not None
            and not await self._catalog_check(model_id)
        ):
            raise Validation(
                "The platform distillation model must be a global catalog model.",
                param="distillation.model_id",
            )

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Begin refreshing in the background. Started by the API and by the worker."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_forever())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        # Shutdown is not a failure, and neither is the refresh loop's last iteration
        # raising as its sleep is cancelled underneath it. `CancelledError` is listed
        # explicitly because it is a `BaseException`, so suppressing `Exception` alone
        # would let the cancellation this very line caused escape into the caller.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A database blip must not take the cached configuration with it: the
                # snapshot in hand is a perfectly good answer until the next tick.
                logger.warning("could not refresh platform settings", exc_info=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _section_schema(name: str) -> type[BaseModel]:
    """The pydantic model behind one section name."""
    annotation = PlatformSettings.model_fields[name].annotation
    assert isinstance(annotation, type) and issubclass(annotation, BaseModel)
    return annotation


def _bootstrap_section(settings: Settings, name: str) -> Mapping[str, Any]:
    section = bootstrap(settings).get(name)
    return section if isinstance(section, Mapping) else {}


def _target(sections: Sequence[str]) -> Target:
    return Target(type="platform_settings", id=None, label=", ".join(sections))


def _snapshot(settings: PlatformSettings, sections: Mapping[str, Any]) -> dict[str, Any]:
    """Only the sections that changed, so the diff is readable.

    A whole-blob before/after would render every section as unchanged noise around the one
    line somebody is looking for, and the audit diff already truncates long values.
    """
    dumped = settings.model_dump(mode="json")
    return {name: dumped.get(name) for name in sections}


def effective_retention(
    config: PlatformSettings, body_days: int, metadata_days: int
) -> tuple[int, int, tuple[str, ...]]:
    """A gateway's retention after the platform ceilings, and which fields moved.

    Returned rather than raised: an organization that lowers the platform ceiling should
    not break every gateway that was configured under the old one. The gateway editor
    shows the capped number and says why — the same treatment
    :class:`~app.services.limits.Effective` gives a rate limit, and for the same reason.
    """
    ceilings: RetentionCeilings = config.retention
    capped: list[str] = []
    bodies = ceilings.cap_body(body_days)
    if bodies != body_days:
        capped.append("retention_days")
    metadata = ceilings.cap_metadata(metadata_days)
    if metadata != metadata_days:
        capped.append("metadata_retention_days")
    # A ceiling pair is validated to be consistent, but a *capped* body window can still
    # end up above an uncapped metadata one when only the second was configured. Bodies
    # outliving the rows describing them is the one combination nothing downstream can
    # represent, so it is closed here rather than left to the next reader.
    bodies = min(bodies, metadata)
    return bodies, metadata, tuple(capped)


__all__ = [
    "REFRESH_SECONDS",
    "SETTINGS_UPDATED",
    "PlatformSettingsService",
    "PlatformView",
    "bootstrap",
    "ceilings_of",
    "effective_retention",
    "resolve",
]
