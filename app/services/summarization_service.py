"""The control-plane half of summarization (task 102): the organization's default model,
and the Monitoring panel's numbers.

Small on purpose. Everything that *does* summarization lives on the pipeline and the
worker; what is here is the two things a screen asks for that are not about one connector
— which model an organization summarizes with when a connector does not say, and what the
whole organization spent on it over a window.

The settings write is the same shape as :class:`~app.services.distillation_service.
DistillationService.update`, on the same column, audited the same way — a knob on the
organization's settings blob, diffed as ``settings.summarization.*``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from app.core.errors import NotFound
from app.core.tenancy import Actor, TenantScope
from app.schemas.config import merge_config
from app.schemas.summarization import (
    ORG_SUMMARIZATION,
    SummarizationDefaults,
    SummarizationSettingsResponse,
    organization_summarization,
)
from app.services.audit_snapshots import subject
from app.services.directory_store import DirectoryStore
from app.services.summarization_store import SummarizationHealth, SummarizationStore
from app.services.summarizer import SummarizationModelResolver

logger = logging.getLogger(__name__)


class SummarizationService:
    def __init__(
        self,
        store: SummarizationStore,
        *,
        directory: DirectoryStore,
        models: SummarizationModelResolver,
    ) -> None:
        self._store = store
        self._directory = directory
        self._models = models

    # -- settings ---------------------------------------------------------

    async def settings(self, actor: Actor) -> SummarizationSettingsResponse:
        organization_id = actor.scope.require_organization()
        config = organization_summarization(await self._stored(organization_id))
        return await self._view(organization_id, config)

    async def update(self, actor: Actor, patch: dict[str, Any]) -> SummarizationSettingsResponse:
        organization_id = actor.scope.require_organization()
        async with self._directory.begin(actor.scope) as transaction:
            organization = await transaction.organization(organization_id)
            if organization is None:  # pragma: no cover - the scope guarantees it
                raise NotFound("No such organization.")
            before = subject(organization)
            settings = dict(organization.settings or {})
            settings[ORG_SUMMARIZATION] = merge_config(
                SummarizationDefaults,
                settings.get(ORG_SUMMARIZATION),
                patch,
                field=ORG_SUMMARIZATION,
            )
            # Reassigned rather than mutated: SQLAlchemy tracks JSONB by identity.
            organization.settings = settings
            transaction.audit(
                actor,
                "organization.summarization.update",
                before=before,
                after=subject(organization),
                organization_id=organization_id,
            )
            await transaction.commit()
        logger.info(
            "summarization settings updated",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "audit_action": "organization.summarization.update",
                "changed": sorted(patch),
            },
        )
        return await self._view(
            organization_id, SummarizationDefaults.load(settings[ORG_SUMMARIZATION])
        )

    # -- health -----------------------------------------------------------

    async def health(
        self,
        actor: Actor,
        *,
        start: datetime,
        end: datetime,
        connector_id: uuid.UUID | None = None,
    ) -> SummarizationHealth:
        """The panel's block over the monitoring page's own window — not a window of its
        own, because summarization happens when documents arrive, and "the last hour" is
        a question with an answer here."""
        async with self._store.begin(actor.scope) as transaction:
            return await transaction.health(start=start, end=end, connector_id=connector_id)

    # -- internals --------------------------------------------------------

    async def _stored(self, organization_id: uuid.UUID) -> dict[str, Any]:
        async with self._directory.begin(
            TenantScope.of_organization(organization_id)
        ) as transaction:
            organization = await transaction.organization(organization_id)
            return dict(getattr(organization, "settings", None) or {})

    async def _view(
        self, organization_id: uuid.UUID, config: SummarizationDefaults
    ) -> SummarizationSettingsResponse:
        explained = await self._models.explain(organization_id)
        return SummarizationSettingsResponse(
            config=config,
            effective_model_id=explained.choice.id if explained else None,
            effective_model_name=explained.choice.name if explained else None,
            effective_model_source=explained.source if explained else None,
        )


__all__ = ["SummarizationService"]
