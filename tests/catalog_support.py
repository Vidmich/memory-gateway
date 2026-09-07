"""Builders and doubles for the model catalog.

The probe is faked by default. What is worth testing about "Test connection" splits in
two, and the two want different tools: *what gets sent* — that the stored credential is
decrypted and handed over, that a draft's own key is used instead — is a question about
:class:`~app.services.catalog.CatalogService`, and :class:`FakeProbe` records the answer.
*What comes back* — a 401 relayed verbatim, a timeout, a body that is not a completion —
is a question about :class:`~app.services.model_probe.ModelProbe`, and
``tests/test_model_probe.py`` answers it against a real socket.

Known plaintexts are here so a test can assert that a secret never appears in a response
by searching the serialized body for it, which is the only check that keeps working when
somebody adds a field years from now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.adapters.base import UpstreamTarget
from app.core.crypto import SecretBox, secret_hint
from app.core.ids import uuid7
from app.db.models import Gateway, GatewayTarget, Organization, UpstreamModel
from app.services.model_probe import ProbeResult

#: Credentials with distinctive, greppable plaintexts. Nothing derives one from the
#: other, so a test that finds either in a response body has found a real leak.
ACME_SECRET = "sk-acme-plaintext-must-never-appear"
PLATFORM_SECRET = "sk-platform-plaintext-must-never-appear"


@dataclass
class FakeProbe:
    """Records the target it was handed and answers with a scripted result."""

    result: ProbeResult = field(
        default_factory=lambda: ProbeResult(ok=True, latency_ms=42, model_echo="upstream-model")
    )
    targets: list[UpstreamTarget] = field(default_factory=list)

    @property
    def last(self) -> UpstreamTarget:
        return self.targets[-1]

    async def run(self, target: UpstreamTarget) -> ProbeResult:
        self.targets.append(target)
        return self.result


def make_model(
    *,
    organization: Organization | None = None,
    name: str = "acme-gpt",
    credential: str | None = None,
    secret_box: SecretBox | None = None,
    **overrides: Any,
) -> UpstreamModel:
    """A model row. ``organization=None`` makes it a global one, as the CHECK requires."""
    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization.id if organization else None,
        "scope": "org" if organization else "global",
        "name": name,
        "description": None,
        "base_url": "https://api.example.com/v1",
        "dialect": "openai",
        "upstream_model_id": "gpt-4o-mini",
        "auth_type": "bearer",
        "extra_headers": {},
        "system_context": None,
        "default_params": {},
        "timeout_seconds": 30,
        "enabled": True,
    }
    values.update(overrides)
    model = UpstreamModel(**values)
    if credential is not None:
        assert secret_box is not None, "a credential needs a SecretBox to encrypt it"
        model.credential_ciphertext = secret_box.encrypt(credential)
        model.credential_hint = secret_hint(credential)
    return model


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
        "system_context": None,
        "param_overrides": {},
    }
    values.update(overrides)
    return Gateway(**values)


def make_target_row(gateway_id: uuid.UUID, model_id: uuid.UUID) -> GatewayTarget:
    return GatewayTarget(
        id=uuid7(),
        gateway_id=gateway_id,
        upstream_model_id=model_id,
        priority=0,
        weight=100,
    )
