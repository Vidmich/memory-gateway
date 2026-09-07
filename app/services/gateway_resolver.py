"""Resolving a gateway slug to everything needed to serve a request.

Three implementations of one port. :class:`DatabaseGatewayResolver` reads PostgreSQL;
:class:`CachedGatewayResolver` wraps it in Redis; tests substitute a fake. The proxy route
knows only :class:`GatewayResolver`, which is why task 02 could ship without a cache and
this task could add one without touching the request path.

**What the cache does not hold.** No API key, ever. A revoked key has to stop working on
the next request, not when a TTL expires, so :class:`~app.services.api_keys.KeyAuthenticator`
reads the row every time — one primary-key lookup, which is cheaper than the correctness
argument for caching it. And the payload stores the provider credential still *encrypted*:
Redis is a cache, not a vault, and there is no reason for a plaintext provider key to exist
in a second system. Decryption happens per request, in memory, as it already did.

**Invalidation is a version counter, not a delete.** ``INCR`` on ``gw:ver:{slug}`` moves
the slug to a new payload key; the old one is simply never read again and expires on its
own. A blind ``DEL`` has a window — a reader that loaded stale rows just before the write
can ``SET`` them back afterwards — and that window produces the worst kind of bug, where
the config is correct everywhere except in production for an unpredictable minute.

The whole cache **fails open** onto the database. A Redis outage should cost latency, not
the data plane.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import GatewayDisabled, GatewayNotFound, GatewayUnavailable
from app.core.crypto import DecryptionError, SecretBox
from app.db.models import Gateway, GatewayTarget
from app.db.scoping import unscoped

logger = logging.getLogger(__name__)

#: Backstop against an invalidation that never happened — a crashed worker, a Redis
#: failover mid-write. Short enough that "why is my change not live" is never the answer
#: to a support ticket, long enough to absorb a burst.
CACHE_TTL_SECONDS = 60

#: Version keys are tiny and one per gateway, but a deleted gateway would leave one
#: behind forever. A week outlives any payload written under it by four orders of
#: magnitude, so a reset to zero can never resurrect one.
VERSION_TTL_SECONDS = 7 * 24 * 3600

PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class ResolvedGateway:
    """A gateway's live configuration, flattened for the request path."""

    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    created_at: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    routing_mode: str = "single"
    system_context: str | None = None
    param_overrides: Mapping[str, Any] = field(default_factory=dict)
    #: Values the client cannot change. Applied after the client's own, which is the
    #: whole difference between a default and a cap — see ``app.services.params``.
    locked_params: Mapping[str, Any] = field(default_factory=dict)
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


class ConfigCache(Protocol):
    """The half of the cache a *write* path touches.

    One method wide on purpose. The control-plane services need to say "this slug's
    configuration changed" and nothing else; giving them the whole cache would let a
    future edit reach for ``put`` from a place that has no business writing one.
    """

    async def invalidate(self, slugs: Iterable[str]) -> None: ...


class GatewayResolver(Protocol):
    """Slug in, live configuration out. Raises rather than returning ``None`` so the
    route never has to decide which failure a missing gateway is."""

    async def resolve(self, slug: str) -> ResolvedGateway: ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class DatabaseGatewayResolver:
    """The source of truth. Two queries' worth of work per call, which is exactly why
    :class:`CachedGatewayResolver` exists in front of it."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_box: SecretBox,
    ) -> None:
        self._session_factory = session_factory
        self._secret_box = secret_box

    async def resolve(self, slug: str) -> ResolvedGateway:
        row = await self._load(slug)
        return self._to_resolved(row)

    async def snapshot(self, slug: str) -> dict[str, Any]:
        """The same read, as the cacheable payload — credentials still encrypted."""
        return _encode(await self._load(slug))

    def rebuild(self, payload: Mapping[str, Any]) -> ResolvedGateway:
        """Turn a cached payload back into a live snapshot, decrypting as it goes."""
        return _decode(payload, self._credential)

    async def _load(self, slug: str) -> Gateway:
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
        return gateway

    def _to_resolved(self, gateway: Gateway) -> ResolvedGateway:
        return _decode(_encode(gateway), self._credential)

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


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


class GatewayCache:
    """The version counter and the payload store, and nothing else.

    Split out from the resolver so invalidation has somewhere to live that the control
    plane can hold without also holding a database session factory — the gateway service
    calls :meth:`invalidate` and knows nothing about how a payload is shaped.
    """

    VERSION_PREFIX = "gw:ver:"
    PAYLOAD_PREFIX = "gw:cfg:"

    def __init__(self, redis: Redis, *, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def version(self, slug: str) -> int:
        raw = await self._redis.get(f"{self.VERSION_PREFIX}{slug}")
        return int(raw) if raw else 0

    async def get(self, slug: str, version: int) -> dict[str, Any] | None:
        raw = await self._redis.get(self._payload_key(slug, version))
        if not raw:
            return None
        try:
            decoded: Any = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(decoded, dict) or decoded.get("payload_version") != PAYLOAD_VERSION:
            # A payload written by a different build. Treated as a miss rather than
            # migrated: the source of truth is one query away.
            return None
        return decoded

    async def put(self, slug: str, version: int, payload: Mapping[str, Any]) -> None:
        await self._redis.set(self._payload_key(slug, version), json.dumps(payload), ex=self._ttl)

    async def invalidate(self, slugs: Iterable[str]) -> None:
        """Move each slug to a new version, so every cached payload for it is orphaned.

        Best-effort by design: if this fails, the TTL still bounds the staleness, and
        refusing the write because a cache could not be poked would be the wrong trade.
        """
        names = [slug for slug in dict.fromkeys(slugs) if slug]
        if not names:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for slug in names:
                    key = f"{self.VERSION_PREFIX}{slug}"
                    pipe.incr(key)
                    pipe.expire(key, VERSION_TTL_SECONDS)
                await pipe.execute()
        except Exception:
            logger.warning(
                "could not invalidate gateway config cache",
                extra={"slugs": names},
                exc_info=True,
            )

    def _payload_key(self, slug: str, version: int) -> str:
        return f"{self.PAYLOAD_PREFIX}{slug}:{version}"


class CachedGatewayResolver:
    """``DatabaseGatewayResolver`` with a Redis read-through cache in front.

    The version is read *before* the database, and the payload is stored under that
    version. A write that lands in between bumps the counter, so what gets stored is
    orphaned rather than served — the stale-resurrection window a delete-based cache has
    does not exist here.
    """

    def __init__(self, source: DatabaseGatewayResolver, cache: GatewayCache) -> None:
        self._source = source
        self._cache = cache

    async def resolve(self, slug: str) -> ResolvedGateway:
        try:
            version = await self._cache.version(slug)
            cached = await self._cache.get(slug, version)
        except Exception:
            self._unavailable()
            return await self._source.resolve(slug)

        if cached is not None:
            return self._source.rebuild(cached)

        # A miss on a slug that does not exist raises out of here without being cached.
        # Negative caching would make "create a gateway and call it" fail for a minute,
        # which is exactly the moment somebody is watching.
        payload = await self._source.snapshot(slug)
        try:
            await self._cache.put(slug, version, payload)
        except Exception:
            self._unavailable()
        return self._source.rebuild(payload)

    def _unavailable(self) -> None:
        logger.warning("gateway config cache unavailable; reading through", exc_info=True)


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
#
# One shape, used for both the cache and the in-process path, so the cached and uncached
# resolvers cannot return subtly different objects. Everything is JSON-native except the
# credential, which stays a base64 *ciphertext* — see the module docstring.


def _encode(gateway: Gateway) -> dict[str, Any]:
    return {
        "payload_version": PAYLOAD_VERSION,
        "id": str(gateway.id),
        "organization_id": str(gateway.organization_id),
        "slug": gateway.slug,
        "name": gateway.name,
        "enabled": gateway.enabled,
        "created_at": gateway.created_at.isoformat(),
        "routing_mode": gateway.routing_mode,
        "system_context": gateway.system_context,
        "param_overrides": dict(gateway.param_overrides or {}),
        "locked_params": dict(gateway.locked_params or {}),
        "targets": [_encode_target(target) for target in _usable(gateway)],
        "disabled": [
            target.upstream_model.name
            for target in gateway.targets
            if not target.upstream_model.enabled
        ],
    }


def _encode_target(target: GatewayTarget) -> dict[str, Any]:
    model = target.upstream_model
    ciphertext = model.credential_ciphertext
    return {
        "id": str(model.id),
        "name": model.name,
        "base_url": model.base_url,
        "dialect": model.dialect,
        "upstream_model_id": model.upstream_model_id,
        "auth_type": model.auth_type,
        "credential_ciphertext": (
            base64.b64encode(ciphertext).decode("ascii") if ciphertext else None
        ),
        "extra_headers": dict(model.extra_headers or {}),
        "system_context": model.system_context,
        "default_params": dict(model.default_params or {}),
        "timeout_seconds": model.timeout_seconds,
    }


def _decode(
    payload: Mapping[str, Any],
    decrypt: Any,
) -> ResolvedGateway:
    if not payload.get("enabled", True):
        # Checked on the way out rather than on the way in, so a disabled gateway is
        # still cacheable — otherwise turning one off would mean a database read per
        # request from every client that has not noticed yet.
        raise GatewayDisabled(
            f"Gateway '{payload['slug']}' is disabled. Enable it under Gateways to "
            f"start serving requests again."
        )
    return ResolvedGateway(
        id=uuid.UUID(payload["id"]),
        organization_id=uuid.UUID(payload["organization_id"]),
        slug=payload["slug"],
        name=payload["name"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        routing_mode=payload.get("routing_mode", "single"),
        system_context=payload.get("system_context"),
        param_overrides=dict(payload.get("param_overrides") or {}),
        locked_params=dict(payload.get("locked_params") or {}),
        targets=tuple(_decode_target(item, decrypt) for item in payload.get("targets", ())),
        disabled=tuple(payload.get("disabled", ())),
    )


def _decode_target(item: Mapping[str, Any], decrypt: Any) -> UpstreamTarget:
    model_id = uuid.UUID(item["id"])
    raw = item.get("credential_ciphertext")
    ciphertext = base64.b64decode(raw) if raw else None
    return UpstreamTarget(
        id=model_id,
        name=item["name"],
        base_url=item["base_url"],
        dialect=item["dialect"],
        upstream_model_id=item["upstream_model_id"],
        auth_type=item["auth_type"],
        credential=decrypt(model_id, ciphertext),
        extra_headers=dict(item.get("extra_headers") or {}),
        system_context=item.get("system_context"),
        default_params=dict(item.get("default_params") or {}),
        timeout_seconds=item["timeout_seconds"],
    )


def _usable(gateway: Gateway) -> list[GatewayTarget]:
    """Enabled targets in priority order. A disabled model is skipped, not an error."""
    return sorted(
        (target for target in gateway.targets if target.upstream_model.enabled),
        key=lambda target: target.priority,
    )


__all__ = [
    "CACHE_TTL_SECONDS",
    "CachedGatewayResolver",
    "DatabaseGatewayResolver",
    "GatewayCache",
    "GatewayResolver",
    "ResolvedGateway",
]
