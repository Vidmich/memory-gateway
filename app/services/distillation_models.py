"""Which model does the remembering (SPEC §6.4, step 2).

An organization picks a model from its own catalog; if it has not, the platform's default
is used. Both are ids rather than names, because a name is unique only within one catalog
and "gpt-4o-mini" exists in most of them — a lookup by name would eventually resolve to a
different organization's row, which is both a bill and a disclosure.

Two things this deliberately does not reuse.

**Not the gateway's target.** A gateway resolves a chain of targets with system contexts,
default parameters and locked overrides; distillation wants one model, with none of that.
Reusing :class:`~app.services.gateway_resolver.ResolvedGateway` would mean an organization's
prompt instructions ("you are a cheerful support agent") were prepended to a fact-extraction
request, which is exactly the sort of thing that produces cheerful facts.

**Not the catalog service.** That is a control-plane object with an actor, an audit trail
and a rate limiter, and this runs on a worker with no actor at all. What it shares is the
decryption, and that lives in :class:`~app.core.crypto.SecretBox`, which both call.

``system_context`` is set to ``None`` here for the reason above, and ``default_params`` is
dropped for a related one: a model configured with ``temperature: 0.9`` for chat should not
make fact extraction creative.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.adapters.base import UpstreamTarget
from app.core.crypto import DecryptionError, SecretBox
from app.core.tenancy import TenantScope
from app.db.models import UpstreamModel
from app.services.catalog_store import CatalogStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """Which model would actually be used, and whether it is the organization's own.

    Here rather than beside the screen that renders it, because the resolution rule — the
    organization's choice, else the platform's, else nothing — is this module's, and the
    two callers that ask about it should not be able to answer differently.
    """

    id: uuid.UUID
    name: str
    from_platform: bool


class CatalogModelResolver:
    """Resolve an organization's distillation model, falling back to the platform's."""

    def __init__(
        self,
        store: CatalogStore,
        *,
        secret_box: SecretBox,
        platform_default_id: uuid.UUID | None = None,
    ) -> None:
        self._store = store
        self._secret_box = secret_box
        self._platform_default_id = platform_default_id

    @property
    def platform_default_id(self) -> uuid.UUID | None:
        return self._platform_default_id

    async def describe(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> ModelChoice | None:
        """Name the model that would distil, without touching its credential.

        A read for a screen, so it does not decrypt: an admin looking at the Settings page
        should not cause a ciphertext to be unwrapped, and a master key that has rotated
        badly should show up when distillation runs rather than when somebody opens a form.
        """
        model = await self._model(organization_id, model_id)
        if model is None:
            return None
        return ModelChoice(
            id=model.id,
            name=model.name,
            from_platform=model_id is None or model.id != model_id,
        )

    async def target(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> UpstreamTarget | None:
        model = await self._model(organization_id, model_id)
        if model is None:
            return None
        if not model.enabled:
            # A model disabled for chat is disabled for this too. Distilling through a
            # model somebody has switched off would be a surprise on the next invoice.
            logger.info(
                "the configured distillation model is disabled",
                extra={"organization_id": str(organization_id), "model_id": str(model.id)},
            )
            return None
        credential = self._decrypt(model)
        if credential is None and model.auth_type != "none":
            return None
        return UpstreamTarget(
            id=model.id,
            name=model.name,
            base_url=model.base_url,
            dialect=model.dialect,
            upstream_model_id=model.upstream_model_id,
            auth_type=model.auth_type,
            credential=credential,
            extra_headers=dict(model.extra_headers or {}),
            system_context=None,
            default_params={},
            timeout_seconds=model.timeout_seconds,
        )

    async def _model(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> UpstreamModel | None:
        wanted = model_id or self._platform_default_id
        if wanted is None:
            return None
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            # `model` rather than `owned_model`: an organization may distil with a global
            # model it does not own, which is the whole point of the platform default.
            found = await transaction.model(wanted)
        if found is None and model_id is not None and self._platform_default_id is not None:
            # The organization's choice has been deleted. Falling back beats stopping: the
            # Settings screen already shows the selection as missing, and silently writing
            # no memory until somebody notices is the worse failure.
            logger.warning(
                "the organization's distillation model no longer exists; using the "
                "platform default",
                extra={"organization_id": str(organization_id), "model_id": str(model_id)},
            )
            return await self._model(organization_id, None)
        return found

    def _decrypt(self, model: UpstreamModel) -> str | None:
        if not model.credential_ciphertext:
            return None
        try:
            return self._secret_box.decrypt(model.credential_ciphertext)
        except DecryptionError:
            # A master key that does not match the one the credential was written under.
            # Returning None here would probe the provider without auth and record the
            # resulting 401 as a distillation failure, which sends the operator after the
            # wrong bug.
            logger.error(
                "could not decrypt the distillation model's credential",
                extra={"model_id": str(model.id)},
            )
            return None


__all__ = ["CatalogModelResolver", "ModelChoice"]
