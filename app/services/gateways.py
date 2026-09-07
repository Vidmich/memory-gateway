"""Resolving a gateway slug to everything needed to serve a request.

Behind an interface on purpose: task 06 puts a Redis cache here, keyed by slug and
invalidated on config change, without the proxy route learning about it. Everything the
resolver returns is a frozen snapshot with credentials already decrypted, so the request
path never touches the ORM or the master key again.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import GatewayNotFound, GatewayUnavailable
from app.core.crypto import DecryptionError, SecretBox
from app.db.models import Gateway, GatewayTarget
from app.db.scoping import unscoped

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedGateway:
    """A gateway's live configuration, flattened for the request path."""

    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    system_context: str | None = None
    param_overrides: Mapping[str, Any] = field(default_factory=dict)
    targets: tuple[UpstreamTarget, ...] = ()
    #: Models this gateway points at that are switched off. Carried so the 503 can say
    #: *which* model is disabled instead of "no usable target" — the difference between
    #: an operator fixing it in one click and going looking for the problem.
    disabled: tuple[str, ...] = ()

    @property
    def virtual_model(self) -> str:
        """The single model name this gateway advertises.

        v1 exposes one virtual model per gateway, named after the slug, so the client's
        ``model`` field never has to change when the org repoints the endpoint at a
        different provider. That is the whole point of the indirection.
        """
        return self.slug

    def target(self) -> UpstreamTarget:
        """The upstream to call.

        Task 08 replaces this with the routing modes; until then a gateway has exactly
        one target and picking it is not a decision.
        """
        if not self.targets:
            raise GatewayUnavailable(self._misconfiguration())
        return self.targets[0]

    def _misconfiguration(self) -> str:
        """Why this gateway cannot serve, in terms the operator can act on.

        A generic "no usable target" is technically true and practically useless: the two
        causes need different fixes, and one of them is a single toggle.
        """
        if self.disabled:
            models = ", ".join(f"'{name}'" for name in self.disabled)
            subject = "model" if len(self.disabled) == 1 else "models"
            return (
                f"Gateway '{self.slug}' points at the disabled upstream {subject} {models}. "
                f"Enable it under Models, or point the gateway at another one."
            )
        return (
            f"Gateway '{self.slug}' has no upstream model configured. "
            f"Add one under Models and point this gateway at it."
        )


class GatewayResolver:
    """Loads gateways from PostgreSQL. The only implementation until task 06."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_box: SecretBox,
    ) -> None:
        self._session_factory = session_factory
        self._secret_box = secret_box

    async def resolve(self, slug: str) -> ResolvedGateway:
        async with self._session_factory() as session:
            statement = (
                select(Gateway)
                .where(Gateway.slug == slug)
                .options(selectinload(Gateway.targets).joinedload(GatewayTarget.upstream_model))
                .execution_options(
                    # The data plane has no control-plane session: the API key that
                    # authenticates the call is itself scoped to this gateway, and the
                    # gateway is what determines the organization (SPEC §5.1).
                    **unscoped("data-plane routing resolves the tenant from the slug")
                )
            )
            gateway = (await session.execute(statement)).scalar_one_or_none()

        if gateway is None:
            raise GatewayNotFound(f"No gateway with slug '{slug}'.")
        if not gateway.enabled:
            raise GatewayUnavailable(f"Gateway '{slug}' is disabled.")

        return ResolvedGateway(
            id=gateway.id,
            organization_id=gateway.organization_id,
            slug=gateway.slug,
            name=gateway.name,
            created_at=gateway.created_at,
            system_context=gateway.system_context,
            param_overrides=dict(gateway.param_overrides or {}),
            targets=tuple(self._to_target(target) for target in _usable(gateway)),
            disabled=tuple(
                target.upstream_model.name
                for target in gateway.targets
                if not target.upstream_model.enabled
            ),
        )

    def _to_target(self, target: GatewayTarget) -> UpstreamTarget:
        model = target.upstream_model
        return UpstreamTarget(
            id=model.id,
            name=model.name,
            base_url=model.base_url,
            dialect=model.dialect,
            upstream_model_id=model.upstream_model_id,
            auth_type=model.auth_type,
            credential=self._credential(model.id, model.credential_ciphertext),
            extra_headers=dict(model.extra_headers or {}),
            system_context=model.system_context,
            default_params=dict(model.default_params or {}),
            timeout_seconds=model.timeout_seconds,
        )

    def _credential(self, model_id: uuid.UUID, ciphertext: bytes | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return self._secret_box.decrypt(ciphertext)
        except DecryptionError:
            # Almost always a master key that does not match the one the credential was
            # written with. Fail the request rather than calling the provider without
            # auth and reporting its 401 as if the client's key were wrong.
            logger.error("could not decrypt upstream credential", extra={"model_id": str(model_id)})
            raise GatewayUnavailable(
                "An upstream model's stored credential could not be decrypted."
            ) from None


def _usable(gateway: Gateway) -> list[GatewayTarget]:
    """Enabled targets in priority order. A disabled model is skipped, not an error."""
    return sorted(
        (target for target in gateway.targets if target.upstream_model.enabled),
        key=lambda target: target.priority,
    )
