"""Gateways and their API keys — the rules that turn configuration into a live endpoint.

Four things here are load-bearing.

**The slug is immutable.** It is a path segment on a public URL that customers have
already deployed, and this system has no way to tell them it changed. A rename from a
settings form would break production traffic silently and instantly, so ``PATCH`` refuses
it and points at cloning instead: a new gateway with the same configuration and a new
slug, which leaves the old one serving until its callers have moved.

**A key's plaintext exists exactly once.** :meth:`GatewayService.create_key` is the only
place the token is ever a value, and it is returned rather than stored — only
``sha256(secret)`` reaches the database. Revocation is a timestamp, never a delete, so
the request log keeps a reference that resolves.

**Revoking a key takes effect on the next request, not on a cache expiry.** Nothing here
caches keys, and :class:`~app.services.api_keys.KeyAuthenticator` reads the row on every
call. The configuration cache is a different thing with a different guarantee, and this
service bumps its version counter on every write that could change what a request does —
including a write to a *model* the gateway points at, which happens in
:mod:`app.services.catalog`.

**Locked parameters are a cap, not a default.** They are stored here and applied in
:func:`app.services.params.resolve_params` *after* the client's own values, which is the
only ordering that makes them mean anything.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core import keys as key_tokens
from app.core.config import Settings, get_settings
from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor
from app.db.models import ApiKey, Gateway, UpstreamModel
from app.db.models.gateway import MAX_SLUG_LENGTH, MIN_SLUG_LENGTH, ROUTING_MODES
from app.schemas.gateway_config import (
    LimitsConfig,
    LoggingConfig,
    MemoryConfig,
    merge_config,
    organization_logging_defaults,
)
from app.services.gateway_probe import GatewayProbe, GatewayProbeResult
from app.services.gateway_resolver import ConfigCache
from app.services.gateway_store import GatewayStore, GatewayTransaction
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of
from app.services.params import validate_params
from app.services.rate_limit import FixedWindowLimiter

logger = logging.getLogger(__name__)

#: Same answer for "no such gateway" and "belongs to another organization". See
#: ``tests/test_cross_tenant.py`` for why the two must be indistinguishable.
NO_SUCH_GATEWAY = "No such gateway."
NO_SUCH_KEY = "No such API key."

#: Slugs that would read as part of the platform rather than as somebody's endpoint.
#: Gateway slugs live under ``/g/``, so none of these actually collides with a route —
#: the reason to refuse them is that ``/g/admin/v1`` and ``/g/g/v1`` look like mistakes
#: in a support ticket, and a slug cannot be renamed once anyone is using it.
RESERVED_SLUGS = frozenset({"api", "admin", "health", "metrics", "g", "www"})

#: Task 08 turns the other two on. Until then, accepting ``failover`` would produce a
#: gateway that quietly serves one target and calls itself something else.
SUPPORTED_ROUTING_MODES = frozenset({"single"})

MAX_KEY_NAME = 100
MAX_KEYS_PER_GATEWAY = 50


class _Unset:
    """Sentinel for "this field was not sent", which ``PATCH`` must distinguish from
    "this field was sent as null"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()

type Maybe[T] = T | _Unset


@dataclass(frozen=True, slots=True)
class GatewayDraft:
    """A new endpoint. ``model_id`` is optional so a gateway can be created and pointed
    at a model afterwards — which is what the editor does when the organization has no
    models yet."""

    name: str
    slug: str
    description: str | None = None
    enabled: bool = True
    routing_mode: str = "single"
    model_id: uuid.UUID | None = None
    system_context: str | None = None
    param_overrides: Mapping[str, Any] = field(default_factory=dict)
    locked_params: Mapping[str, Any] = field(default_factory=dict)
    memory_config: Mapping[str, Any] = field(default_factory=dict)
    logging_config: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GatewayPatch:
    """A partial update. There is deliberately no ``slug`` — see the module docstring.

    The three config blobs are *merged*, not replaced: sending ``{"doc_top_k": 8}`` under
    ``memory_config`` changes one knob, so a form that only knows about one section
    cannot wipe the settings of another.
    """

    name: Maybe[str] = UNSET
    description: Maybe[str | None] = UNSET
    enabled: Maybe[bool] = UNSET
    routing_mode: Maybe[str] = UNSET
    model_id: Maybe[uuid.UUID | None] = UNSET
    system_context: Maybe[str | None] = UNSET
    param_overrides: Maybe[Mapping[str, Any]] = UNSET
    locked_params: Maybe[Mapping[str, Any]] = UNSET
    memory_config: Maybe[Mapping[str, Any]] = UNSET
    logging_config: Maybe[Mapping[str, Any]] = UNSET
    limits: Maybe[Mapping[str, Any]] = UNSET


@dataclass(frozen=True, slots=True)
class GatewayView:
    """A gateway plus the things a screen needs and the row does not carry."""

    gateway: Gateway
    #: The models it routes to, resolved. One entry until task 08.
    models: tuple[UpstreamModel, ...]
    #: Active keys. Shown on the list so "why is nobody calling this" has an answer.
    key_count: int
    endpoint_url: str


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """The one moment a key's plaintext exists. ``token`` is never stored or returned
    again — the row keeps only its hash."""

    key: ApiKey
    token: str


class GatewayService:
    def __init__(
        self,
        store: GatewayStore,
        *,
        probe: GatewayProbe,
        cache: ConfigCache | None = None,
        test_limiter: FixedWindowLimiter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._store = store
        self._probe = probe
        self._cache = cache
        self._limiter = test_limiter
        self._settings = settings or get_settings()

    # -- reads ------------------------------------------------------------

    async def list_gateways(
        self,
        actor: Actor,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[GatewayView]:
        size = clamp_limit(limit)
        after = decode_cursor(cursor)

        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.gateways(after=after, limit=size)
            page = page_of(rows, limit=size, cursor_of=lambda row: row.id)
            counts = await transaction.key_counts([row.id for row in page.items])
            views = tuple(
                self._view(gateway, key_count=counts.get(gateway.id, 0)) for gateway in page.items
            )

        return Page(items=views, next_cursor=page.next_cursor)

    async def get_gateway(self, actor: Actor, gateway_id: uuid.UUID) -> GatewayView:
        async with self._store.begin(actor.scope) as transaction:
            gateway = await self._must_find(transaction, gateway_id)
            counts = await transaction.key_counts([gateway.id])
            return self._view(gateway, key_count=counts.get(gateway.id, 0))

    async def list_keys(self, actor: Actor, gateway_id: uuid.UUID) -> tuple[ApiKey, ...]:
        async with self._store.begin(actor.scope) as transaction:
            gateway = await self._must_find(transaction, gateway_id)
            return tuple(await transaction.keys(gateway.id))

    # -- writes -----------------------------------------------------------

    async def create_gateway(self, actor: Actor, draft: GatewayDraft) -> GatewayView:
        slug = _check_slug(draft.slug)
        _check_routing_mode(draft.routing_mode)
        overrides = validate_params(draft.param_overrides, field="param_overrides")
        locked = validate_params(draft.locked_params, field="locked_params")

        async with self._store.begin(actor.scope) as transaction:
            if await transaction.slug_taken(slug):
                # Globally unique, so this can collide with another organization's
                # gateway. The message says the slug is taken and nothing about who has
                # it — which is all a caller needs and all they may know.
                raise Conflict(
                    f"The slug '{slug}' is already in use. Slugs are unique across the "
                    f"whole platform because they appear in the endpoint URL.",
                    param="slug",
                )

            gateway = Gateway(
                id=uuid7(),
                slug=slug,
                name=draft.name.strip(),
                description=draft.description,
                enabled=draft.enabled,
                routing_mode=draft.routing_mode,
                system_context=draft.system_context,
                param_overrides=overrides,
                locked_params=locked,
                memory_config=merge_config(
                    MemoryConfig, {}, draft.memory_config, field="memory_config"
                ),
                # SPEC §10.2: an organization may set stricter defaults than the
                # platform's, and a new gateway inherits them. Merged *under* the draft,
                # so a form that sends its own value still wins — this is a starting
                # point, not a ceiling. Whether it should also be a ceiling is a real
                # question and a different feature; the work item asks for defaults.
                logging_config=merge_config(
                    LoggingConfig,
                    organization_logging_defaults(await transaction.organization_settings()),
                    draft.logging_config,
                    field="logging_config",
                ),
                limits=merge_config(LimitsConfig, {}, draft.limits, field="limits"),
            )
            await transaction.add_gateway(gateway)

            await transaction.set_targets(
                gateway, await self._resolve_targets(transaction, draft.model_id)
            )
            await transaction.commit()

            view = self._view(gateway, key_count=0)

        await self._invalidate(gateway.slug)
        self._log("gateway created", actor, gateway, action="gateway.create")
        return view

    async def update_gateway(
        self, actor: Actor, gateway_id: uuid.UUID, patch: GatewayPatch
    ) -> GatewayView:
        if not isinstance(patch.routing_mode, _Unset):
            _check_routing_mode(patch.routing_mode)

        async with self._store.begin(actor.scope) as transaction:
            gateway = await self._must_find(transaction, gateway_id)

            _apply(gateway, "name", _stripped(patch.name))
            _apply(gateway, "description", patch.description)
            _apply(gateway, "enabled", patch.enabled)
            _apply(gateway, "routing_mode", patch.routing_mode)
            _apply(gateway, "system_context", patch.system_context)
            if not isinstance(patch.param_overrides, _Unset):
                gateway.param_overrides = validate_params(
                    patch.param_overrides, field="param_overrides"
                )
            if not isinstance(patch.locked_params, _Unset):
                gateway.locked_params = validate_params(patch.locked_params, field="locked_params")
            if not isinstance(patch.memory_config, _Unset):
                gateway.memory_config = merge_config(
                    MemoryConfig, gateway.memory_config, patch.memory_config, field="memory_config"
                )
            if not isinstance(patch.logging_config, _Unset):
                gateway.logging_config = merge_config(
                    LoggingConfig,
                    gateway.logging_config,
                    patch.logging_config,
                    field="logging_config",
                )
            if not isinstance(patch.limits, _Unset):
                gateway.limits = merge_config(
                    LimitsConfig, gateway.limits, patch.limits, field="limits"
                )

            if not isinstance(patch.model_id, _Unset):
                await transaction.set_targets(
                    gateway, await self._resolve_targets(transaction, patch.model_id)
                )

            await transaction.commit()
            counts = await transaction.key_counts([gateway.id])
            view = self._view(gateway, key_count=counts.get(gateway.id, 0))

        await self._invalidate(gateway.slug)
        self._log("gateway updated", actor, gateway, action="gateway.update")
        return view

    async def delete_gateway(self, actor: Actor, gateway_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            gateway = await self._must_find(transaction, gateway_id)
            slug, name = gateway.slug, gateway.name
            organization_id = gateway.organization_id
            # Keys and targets go with it: `ON DELETE CASCADE` on both. A key without a
            # gateway could never authenticate anything, so keeping it would only make
            # the revocation list longer.
            await transaction.delete_gateway(gateway)
            await transaction.commit()

        await self._invalidate(slug)
        logger.info(
            "gateway deleted",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(organization_id),
                "gateway_id": str(gateway_id),
                "gateway_slug": slug,
                "gateway_name": name,
                "audit_action": "gateway.delete",
            },
        )

    # -- keys -------------------------------------------------------------

    async def create_key(
        self,
        actor: Actor,
        gateway_id: uuid.UUID,
        *,
        name: str,
        expires_at: datetime | None = None,
    ) -> IssuedKey:
        """Mint a key and return its plaintext. This is the only time it exists."""
        label = name.strip()
        if not label or len(label) > MAX_KEY_NAME:
            raise Validation(
                f"A key needs a name of at most {MAX_KEY_NAME} characters. "
                f"It is how you tell them apart when one has to be revoked.",
                param="name",
            )
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise Validation("An expiry date has to be in the future.", param="expires_at")

        async with self._store.begin(actor.scope) as transaction:
            gateway = await self._must_find(transaction, gateway_id)

            existing = await transaction.keys(gateway.id)
            active = [key for key in existing if key.revoked_at is None]
            if len(active) >= MAX_KEYS_PER_GATEWAY:
                raise Conflict(
                    f"This gateway already has {MAX_KEYS_PER_GATEWAY} active keys. "
                    f"Revoke one before creating another."
                )

            # The row id is minted first because it is *inside* the token — that is what
            # makes authenticating one a single primary-key lookup (app/core/keys.py).
            minted = key_tokens.mint(uuid7())
            key = ApiKey(
                id=minted.key_id,
                gateway_id=gateway.id,
                name=label,
                key_hash=minted.key_hash,
                prefix=minted.prefix,
                expires_at=expires_at,
            )
            await transaction.add_key(key)
            await transaction.commit()

        logger.info(
            "api key created",
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(gateway.organization_id),
                "gateway_id": str(gateway.id),
                "api_key_id": str(key.id),
                "audit_action": "key.create",
            },
        )
        return IssuedKey(key=key, token=minted.token)

    async def revoke_key(self, actor: Actor, key_id: uuid.UUID) -> ApiKey:
        """Soft. The row stays so the request log can still say which key made a call.

        No cache to clear: keys are never cached, precisely so this takes effect on the
        next request rather than at the end of some TTL.
        """
        async with self._store.begin(actor.scope) as transaction:
            key = await transaction.key(key_id)
            if key is None:
                raise NotFound(NO_SUCH_KEY)
            if key.revoked_at is None:
                key.revoked_at = datetime.now(UTC)
            await transaction.commit()

        logger.info(
            "api key revoked",
            extra={
                "user_id": str(actor.user_id),
                "gateway_id": str(key.gateway_id),
                "api_key_id": str(key.id),
                "audit_action": "key.revoke",
            },
        )
        return key

    # -- connectivity -----------------------------------------------------

    async def test_gateway(
        self, actor: Actor, gateway_id: uuid.UUID, *, message: str
    ) -> GatewayProbeResult:
        """Send a probe completion **through the real proxy path**.

        Through the resolver, not through this service's own read: the point is to
        exercise what a customer's request exercises, cache included. A green result here
        that a real call contradicts would be worse than no button at all.
        """
        if self._limiter is not None:
            await self._limiter.check(str(actor.user_id))

        async with self._store.begin(actor.scope) as transaction:
            gateway = await self._must_find(transaction, gateway_id)
            slug = gateway.slug

        result = await self._probe.run(slug, message=message)
        logger.info(
            "gateway tested",
            extra={
                "user_id": str(actor.user_id),
                "gateway_id": str(gateway_id),
                "gateway_slug": slug,
                "ok": result.ok,
                "total_ms": result.total_ms,
                "audit_action": "gateway.test",
            },
        )
        return result

    # -- internals --------------------------------------------------------

    def _view(self, gateway: Gateway, *, key_count: int) -> GatewayView:
        models = tuple(
            target.upstream_model for target in gateway.targets if target.upstream_model is not None
        )
        return GatewayView(
            gateway=gateway,
            models=models,
            key_count=key_count,
            endpoint_url=self.endpoint_url(gateway.slug),
        )

    def endpoint_url(self, slug: str) -> str:
        """What a customer pastes into an OpenAI client's ``base_url``.

        Built from ``PUBLIC_BASE_URL`` on the server rather than from the browser's own
        origin: behind an ingress the two differ, and the URL shown next to a copy button
        has to be the one that works from outside.
        """
        return f"{self._settings.public_base_url.rstrip('/')}/g/{slug}/v1"

    async def _must_find(self, transaction: GatewayTransaction, gateway_id: uuid.UUID) -> Gateway:
        gateway = await transaction.gateway(gateway_id)
        if gateway is None:
            raise NotFound(NO_SUCH_GATEWAY)
        return gateway

    async def _resolve_targets(
        self, transaction: GatewayTransaction, model_id: uuid.UUID | None
    ) -> list[UpstreamModel]:
        """Check the model is one this caller may point at, and return the chain.

        A 422 rather than a 404: the id came from a form field, and naming the field is
        what lets the UI put the message on the model picker. It is checked against the
        *visible* catalog — own models plus global ones — which is the same set the
        picker was populated from (SPEC §5.3).
        """
        if model_id is None:
            return []
        model = await transaction.visible_model(model_id)
        if model is None:
            raise Validation(
                "That model is not available to this organization. Pick one from the "
                "Models screen, or add it there first.",
                param="model_id",
            )
        return [model]

    async def _invalidate(self, slug: str) -> None:
        if self._cache is not None:
            await self._cache.invalidate([slug])

    def _log(self, message: str, actor: Actor, gateway: Gateway, *, action: str) -> None:
        logger.info(
            message,
            extra={
                "user_id": str(actor.user_id),
                "organization_id": str(gateway.organization_id),
                "gateway_id": str(gateway.id),
                "gateway_slug": gateway.slug,
                "audit_action": action,
            },
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_slug(slug: str) -> str:
    """The one validation with a permanent consequence, so the message says so."""
    value = slug.strip().lower()
    # Reserved first: some of them are also too short, and "reserved" is the more useful
    # of two true statements — it says the name will never be available, not that a
    # longer version of it might be.
    if value in RESERVED_SLUGS:
        raise Validation(
            f"'{value}' is reserved. Pick something that names the endpoint, like 'acme-support'.",
            param="slug",
        )
    if not (MIN_SLUG_LENGTH <= len(value) <= MAX_SLUG_LENGTH):
        raise Validation(
            f"A slug is between {MIN_SLUG_LENGTH} and {MAX_SLUG_LENGTH} characters.",
            param="slug",
        )
    if not _is_url_safe(value):
        raise Validation(
            "A slug is lower-case letters, digits and hyphens, and cannot start or end "
            "with a hyphen. It appears in the endpoint URL.",
            param="slug",
        )
    return value


#: The same shape as the ``slug_is_url_safe`` CHECK on the table. Written twice on
#: purpose: this one produces a sentence somebody can act on, the constraint catches
#: anything that never came through here.
_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _is_url_safe(value: str) -> bool:
    return _SLUG.match(value) is not None


def _check_routing_mode(mode: str) -> None:
    if mode not in ROUTING_MODES:
        raise Validation(
            f"Routing mode must be one of {', '.join(ROUTING_MODES)}.", param="routing_mode"
        )
    if mode not in SUPPORTED_ROUTING_MODES:
        # The column accepts it and the UI names it, so that "can this gateway fail
        # over?" is answerable from the screen. Task 08 makes it work, and this check is
        # the only thing that has to change.
        raise Validation(
            f"The '{mode}' routing mode is not yet supported by this build. "
            f"Available: {', '.join(sorted(SUPPORTED_ROUTING_MODES))}.",
            param="routing_mode",
        )


def _stripped(value: Maybe[str]) -> Maybe[str]:
    return value if isinstance(value, _Unset) else value.strip()


def _apply(gateway: Gateway, attribute: str, value: Maybe[Any]) -> None:
    if not isinstance(value, _Unset):
        setattr(gateway, attribute, value)


__all__ = [
    "RESERVED_SLUGS",
    "UNSET",
    "GatewayDraft",
    "GatewayPatch",
    "GatewayService",
    "GatewayView",
    "IssuedKey",
    "Maybe",
]
