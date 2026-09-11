"""The gateway config cache: what it holds, what it refuses to hold, and how it forgets.

Against a hand-written Redis double rather than a real server, because every property
worth asserting here is about *our* protocol — which key the payload lands under, that the
version moves on a write, that a credential is never stored in the clear — and none of
them is about Redis. The one thing a double could hide is a command that does not exist,
and the four used here (``get``, ``set``, ``incr``, ``expire``) are the four every Redis
has had for fifteen years.

The stale-resurrection test is the one to read. It is the reason invalidation is a version
counter and not a delete.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.services.gateway_resolver import (
    CACHE_TTL_SECONDS,
    PAYLOAD_VERSION,
    CachedGatewayResolver,
    GatewayCache,
    ResolvedGateway,
)
from app.services.templates import DEFAULT_TEMPLATES
from tests.catalog_support import ACME_SECRET

# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class FakeRedis:
    """Enough Redis for this cache: strings, a counter, and a no-op TTL."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("redis is down")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        self._check()
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def incr(self, key: str) -> int:
        self._check()
        nxt = int(self.values.get(key, "0")) + 1
        self.values[key] = str(nxt)
        return nxt

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        self._check()
        self.expiries[key] = seconds
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


@dataclass
class FakePipeline:
    redis: FakeRedis
    queued: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incr(self, key: str) -> None:
        self.queued.append(("incr", (key,)))

    def expire(self, key: str, seconds: int, nx: bool = False) -> None:
        self.queued.append(("expire", (key, seconds)))

    async def execute(self) -> list[Any]:
        results = []
        for name, args in self.queued:
            results.append(await getattr(self.redis, name)(*args))
        self.queued.clear()
        return results


@dataclass
class FakeSource:
    """Stands in for :class:`DatabaseGatewayResolver`, counting its loads."""

    payload: dict[str, Any]
    loads: int = 0

    async def snapshot(self, slug: str) -> dict[str, Any]:
        self.loads += 1
        return dict(self.payload)

    async def resolve(self, slug: str) -> ResolvedGateway:
        self.loads += 1
        return self.rebuild(self.payload)

    def rebuild(self, payload: dict[str, Any]) -> ResolvedGateway:
        from app.services.gateway_resolver import _decode

        return _decode(payload, lambda model_id, ciphertext: "decrypted")


def payload_for(slug: str = "acme-chat", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        # The constant, not a literal: bumping it is how a build refuses payloads it
        # cannot read, and a fixture pinned to 1 would quietly stop testing the cache
        # at all — every read would be a miss and every assertion would still pass.
        "payload_version": PAYLOAD_VERSION,
        "id": str(uuid7()),
        "organization_id": str(uuid7()),
        "slug": slug,
        "name": "Acme Chat",
        "enabled": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "routing_mode": "single",
        "system_context": "Be concise.",
        "param_overrides": {},
        "locked_params": {},
        "targets": [],
        "weights": {},
        "disabled": [],
        "logging": {
            "request_body": True,
            "assembled_prompt": True,
            "response_body": True,
            "redaction_patterns": [],
        },
    }
    body.update(overrides)
    return body


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(redis: FakeRedis) -> GatewayCache:
    return GatewayCache(redis)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# read-through
# ---------------------------------------------------------------------------


async def test_the_first_read_loads_from_the_source(cache: GatewayCache) -> None:
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    gateway = await resolver.resolve("acme-chat")

    assert gateway.slug == "acme-chat"
    assert source.loads == 1


async def test_the_second_read_does_not(cache: GatewayCache) -> None:
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")
    await resolver.resolve("acme-chat")

    assert source.loads == 1


async def test_the_payload_carries_a_ttl(cache: GatewayCache, redis: FakeRedis) -> None:
    """The backstop against an invalidation that never happened."""
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")

    assert set(redis.expiries.values()) == {CACHE_TTL_SECONDS}


# ---------------------------------------------------------------------------
# invalidation
# ---------------------------------------------------------------------------


async def test_invalidating_forces_a_reload(cache: GatewayCache) -> None:
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")
    await cache.invalidate(["acme-chat"])
    await resolver.resolve("acme-chat")

    assert source.loads == 2


async def test_a_change_is_visible_on_the_very_next_request(cache: GatewayCache) -> None:
    """ "Editing the system prompt affects the very next request" — the acceptance
    criterion, expressed against the thing that could break it."""
    source = FakeSource(payload_for(system_context="Be concise."))
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")
    source.payload = payload_for(system_context="Be verbose.")
    await cache.invalidate(["acme-chat"])

    assert (await resolver.resolve("acme-chat")).system_context == "Be verbose."


async def test_invalidating_another_slug_does_not_evict_this_one(cache: GatewayCache) -> None:
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")
    await cache.invalidate(["globex-chat"])
    await resolver.resolve("acme-chat")

    assert source.loads == 1


async def test_a_concurrent_write_cannot_resurrect_stale_config(
    cache: GatewayCache,
) -> None:
    """The reason invalidation is a version counter rather than a delete.

    A reader that loaded the old rows *before* a write, and stores them *after* it, writes
    them under the version it read — which nothing looks up any more. With a blind delete
    the same interleaving puts stale config back into a key the next request reads, and it
    stays there for the length of the TTL.
    """
    source = FakeSource(payload_for(system_context="old"))
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    stale_version = await cache.version("acme-chat")  # what a slow reader would have read
    await cache.invalidate(["acme-chat"])  # the write lands
    await cache.put("acme-chat", stale_version, payload_for(system_context="old"))  # slow SET

    source.payload = payload_for(system_context="new")
    assert (await resolver.resolve("acme-chat")).system_context == "new"


# ---------------------------------------------------------------------------
# what is never cached
# ---------------------------------------------------------------------------


async def test_a_credential_is_stored_encrypted(cache: GatewayCache, redis: FakeRedis) -> None:
    """Redis is a cache, not a vault. The payload holds the ciphertext and decryption
    happens per request, in memory, exactly as it did before there was a cache."""
    box = SecretBox.from_settings()
    target: dict[str, Any] = {
        "id": str(uuid7()),
        "name": "acme-gpt",
        "base_url": "https://api.example.com/v1",
        "dialect": "openai",
        "upstream_model_id": "gpt-4o-mini",
        "auth_type": "bearer",
        "credential_ciphertext": base64.b64encode(box.encrypt(ACME_SECRET)).decode("ascii"),
        "extra_headers": {},
        "system_context": None,
        "default_params": {},
        "timeout_seconds": 30,
    }
    source = FakeSource(payload_for(targets=[target]))
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")

    stored = "".join(redis.values.values())
    assert ACME_SECRET not in stored
    assert stored.count("credential_ciphertext") == 1


async def test_the_cache_holds_no_api_keys(cache: GatewayCache, redis: FakeRedis) -> None:
    """Asserted as a property of the payload's shape. A revoked key has to stop working on
    the next request, and the only way to guarantee that is for the key never to be here.
    """
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    await resolver.resolve("acme-chat")
    payloads = [json.loads(value) for value in redis.values.values() if value.startswith("{")]

    assert all("keys" not in payload and "key_hash" not in payload for payload in payloads)


# ---------------------------------------------------------------------------
# failing open
# ---------------------------------------------------------------------------


async def test_a_redis_outage_reads_through_to_the_database(
    cache: GatewayCache, redis: FakeRedis
) -> None:
    """A cache outage should cost latency, not the data plane."""
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]
    redis.fail = True

    gateway = await resolver.resolve("acme-chat")

    assert gateway.slug == "acme-chat"


async def test_a_failed_invalidation_does_not_raise(cache: GatewayCache, redis: FakeRedis) -> None:
    """Refusing a configuration save because a cache could not be poked would be the wrong
    trade — the TTL still bounds how long the staleness lasts."""
    redis.fail = True

    await cache.invalidate(["acme-chat"])


async def test_the_routing_weights_survive_the_round_trip(cache: GatewayCache) -> None:
    """The failure this guards against is silent and expensive: a payload that loses its
    weights sends an A/B split to whichever target the fallback picks, and every number on
    the comparison screen is then about the wrong thing."""
    model_id = uuid7()
    source = FakeSource(
        payload_for(
            routing_mode="ab_split",
            targets=[
                {
                    "id": str(model_id),
                    "name": "acme-gpt",
                    "base_url": "https://api.example.com/v1",
                    "dialect": "openai",
                    "upstream_model_id": "gpt-4o-mini",
                    "auth_type": "bearer",
                    "credential_ciphertext": None,
                    "extra_headers": {},
                    "system_context": None,
                    "default_params": {},
                    "timeout_seconds": 60,
                }
            ],
            weights={str(model_id): 70},
        )
    )
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    first = await resolver.resolve("acme-chat")
    second = await resolver.resolve("acme-chat")

    assert first.weights == {model_id: 70}
    # The second read comes out of Redis, through JSON, where the key was a string.
    assert second.weights == {model_id: 70}
    assert source.loads == 1


async def test_the_templates_survive_the_round_trip(cache: GatewayCache) -> None:
    """Task 105. The nine strings ride on the payload as a plain object; a payload
    without them — or with a key the build does not know — decodes to the defaults."""
    source = FakeSource(
        payload_for(templates={"reference_heading": "## Referenzmaterial", "extra": "ignored"})
    )
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    first = await resolver.resolve("acme-chat")
    second = await resolver.resolve("acme-chat")

    assert first.templates.reference_heading == "## Referenzmaterial"
    assert second.templates == first.templates
    assert second.templates.excerpt == DEFAULT_TEMPLATES.excerpt
    assert second.template_fingerprint != DEFAULT_TEMPLATES.fingerprint
    assert source.loads == 1

    # Another slug, so the read is not served from the payload cached above.
    other = FakeSource(payload_for(slug="acme-plain"))
    plain = await CachedGatewayResolver(other, cache).resolve("acme-plain")  # type: ignore[arg-type]
    assert plain.templates == DEFAULT_TEMPLATES


async def test_a_payload_from_another_build_is_treated_as_a_miss(
    cache: GatewayCache, redis: FakeRedis
) -> None:
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]
    await resolver.resolve("acme-chat")

    for key in list(redis.values):
        if key.startswith(GatewayCache.PAYLOAD_PREFIX):
            redis.values[key] = json.dumps({"payload_version": 99, "slug": "acme-chat"})

    await resolver.resolve("acme-chat")

    assert source.loads == 2


async def test_unparseable_json_is_treated_as_a_miss(cache: GatewayCache, redis: FakeRedis) -> None:
    source = FakeSource(payload_for())
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]
    await resolver.resolve("acme-chat")

    for key in list(redis.values):
        if key.startswith(GatewayCache.PAYLOAD_PREFIX):
            redis.values[key] = "not json"

    await resolver.resolve("acme-chat")

    assert source.loads == 2


# ---------------------------------------------------------------------------
# disabled gateways
# ---------------------------------------------------------------------------


async def test_a_disabled_gateway_is_a_403_even_from_the_cache(cache: GatewayCache) -> None:
    """The flag is checked on the way *out* of the cache, not on the way in, so turning a
    gateway off does not turn every request from a client that has not noticed into a
    database read."""
    from app.api.proxy.errors import GatewayDisabled

    source = FakeSource(payload_for(enabled=False))
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    with pytest.raises(GatewayDisabled):
        await resolver.resolve("acme-chat")
    with pytest.raises(GatewayDisabled):
        await resolver.resolve("acme-chat")


# ---------------------------------------------------------------------------
# the acceptance criterion the cache could break
# ---------------------------------------------------------------------------


async def test_revoking_a_key_stops_the_next_request_even_with_a_warm_cache(
    cache: GatewayCache, upstream: Any
) -> None:
    """The whole reason keys are not cached, tested end to end over HTTP.

    The gateway's *configuration* is served from Redis — asserted by the load count, so
    this cannot pass with a cold cache and look the same. The key is read every time, so
    the revocation lands on the very next request rather than at the end of a TTL.
    """
    from httpx import ASGITransport, AsyncClient

    from tests.conftest import build_proxy_app
    from tests.monitoring_support import build_logs
    from tests.support import Behaviour, FakeAuthenticator, completion

    target: dict[str, Any] = {
        "id": str(uuid7()),
        "name": "acme-gpt",
        "base_url": f"{upstream.base_url}/v1",
        "dialect": "openai",
        "upstream_model_id": "upstream-model",
        "auth_type": "bearer",
        "credential_ciphertext": None,
        "extra_headers": {},
        "system_context": None,
        "default_params": {},
        "timeout_seconds": 5,
    }
    body = payload_for(targets=[target])
    source = FakeSource(body)
    resolver = CachedGatewayResolver(source, cache)  # type: ignore[arg-type]

    authenticator = FakeAuthenticator()
    token = authenticator.issue(uuid.UUID(body["id"]))

    app = build_proxy_app(resolver, authenticator, build_logs())  # type: ignore[arg-type]
    upstream.behaviour = Behaviour(body=completion())

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            url = "/g/acme-chat/v1/chat/completions"
            request = {"model": "acme-chat", "messages": [{"role": "user", "content": "hi"}]}
            headers = {"Authorization": f"Bearer {token}"}

            first = await client.post(url, json=request, headers=headers)

            # Revoke, and change nothing else: the config cache is deliberately left warm.
            key_id = next(iter(authenticator.records))
            key_hash, owner, _ = authenticator.records[key_id]
            authenticator.records[key_id] = (key_hash, owner, datetime.now(UTC))

            second = await client.post(url, json=request, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert "revoked" in second.json()["error"]["message"]
    assert source.loads == 1, "the config cache was not warm, so this proved nothing"
