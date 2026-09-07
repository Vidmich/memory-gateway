"""Builders and doubles for gateways and API keys.

The probe is faked by default, and the split is the same one the model catalog makes.
*What the button asks for* — that it goes through the resolver, that it reports the slug's
real configuration — is a question about :class:`~app.services.gateways.GatewayService`,
and :class:`FakeGatewayProbe` records the answer. *What the probe actually does* — prompt
assembly, the parameter merge, an upstream 401 relayed verbatim — is a question about
:class:`~app.services.gateway_probe.ProxyGatewayProbe`, and ``tests/test_gateway_probe.py``
answers it against a real socket.

:class:`RecordingCache` is the same idea for invalidation: the interesting assertion is
"a write bumped the version for this slug", which is a fact about the service, not about
Redis. ``tests/test_gateway_cache.py`` exercises the real one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core import keys as key_tokens
from app.core.ids import uuid7
from app.db.models import ApiKey, Gateway, Organization
from app.services.gateway_probe import GatewayProbeResult, PromptMessage


@dataclass
class FakeGatewayProbe:
    """Records the slug and message it was handed, and answers with a scripted result."""

    result: GatewayProbeResult = field(
        default_factory=lambda: GatewayProbeResult(
            ok=True,
            total_ms=12,
            upstream_ms=9,
            assembled_prompt=(PromptMessage(role="user", content="hi"),),
            model_name="upstream-model",
            content="hello",
        )
    )
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def last(self) -> tuple[str, str]:
        return self.calls[-1]

    async def run(self, slug: str, *, message: str) -> GatewayProbeResult:
        self.calls.append((slug, message))
        return self.result


@dataclass
class RecordingCache:
    """Stands in for :class:`~app.services.gateway_resolver.GatewayCache`.

    Only :meth:`invalidate` is used by the services, which is the point — the port a write
    path touches is one method wide.
    """

    invalidated: list[str] = field(default_factory=list)

    async def invalidate(self, slugs: Any) -> None:
        self.invalidated.extend(slugs)


def make_gateway_row(
    organization: Organization, *, slug: str = "demo", **overrides: Any
) -> Gateway:
    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization.id,
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "description": None,
        "enabled": True,
        "routing_mode": "single",
        "system_context": None,
        "param_overrides": {},
        "locked_params": {},
        "memory_config": {},
        "logging_config": {},
        "limits": {},
    }
    values.update(overrides)
    return Gateway(**values)


def make_key_row(
    gateway_id: uuid.UUID,
    *,
    name: str = "default",
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """A key row and its plaintext — the pairing only a test gets to keep."""
    minted = key_tokens.mint(uuid7())
    key = ApiKey(
        id=minted.key_id,
        gateway_id=gateway_id,
        name=name,
        key_hash=minted.key_hash,
        prefix=minted.prefix,
        revoked_at=revoked_at,
        expires_at=expires_at,
    )
    return key, minted.token


__all__ = ["FakeGatewayProbe", "RecordingCache", "make_gateway_row", "make_key_row"]
