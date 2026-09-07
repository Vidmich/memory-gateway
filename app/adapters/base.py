"""The upstream adapter seam.

An adapter is the only place that knows a provider's wire format. Adding ``bedrock`` or
``vertex`` later means one new class and one registry entry (SPEC §8.3).

Adapters take a :class:`UpstreamTarget` — a plain frozen record — rather than the ORM
model. That keeps them unit-testable without a database, and keeps the decrypted
credential out of a mapped object that could otherwise be serialised somewhere it should
not go.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.schemas.openai import ChatRequest, ChatResponse, StreamFrame


@dataclass(frozen=True)
class UpstreamTarget:
    """A resolved upstream model: everything needed to make the call, nothing more."""

    id: uuid.UUID
    name: str
    base_url: str
    dialect: str
    upstream_model_id: str
    auth_type: str = "none"
    credential: str | None = None
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    system_context: str | None = None
    default_params: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 60

    def __repr__(self) -> str:
        # The default dataclass repr would put the decrypted credential into any log line
        # or traceback that happens to include a target.
        return f"UpstreamTarget(id={self.id}, name={self.name!r}, dialect={self.dialect!r})"


class UpstreamAdapter(Protocol):
    """Translate between the gateway's OpenAI-shaped view and one provider's dialect.

    ``request`` is the *outbound* request: prompt layers already applied and parameters
    already merged. An adapter decides wire format and auth, not policy.
    """

    dialect: str

    def prepare(self, request: ChatRequest, target: UpstreamTarget) -> httpx.Request: ...

    def parse(self, response: httpx.Response) -> ChatResponse: ...

    def parse_stream(self, response: httpx.Response) -> AsyncIterator[StreamFrame]: ...


_ADAPTERS: dict[str, UpstreamAdapter] = {}


def register(adapter: UpstreamAdapter) -> UpstreamAdapter:
    _ADAPTERS[adapter.dialect] = adapter
    return adapter


def get_adapter(dialect: str) -> UpstreamAdapter:
    try:
        return _ADAPTERS[dialect]
    except KeyError:
        raise ValueError(f"no adapter for dialect {dialect!r}") from None


def known_dialects() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
