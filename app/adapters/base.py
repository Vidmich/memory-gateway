"""The upstream adapter seam.

An adapter is the only place that knows a provider's wire format. Adding ``bedrock`` or
``vertex`` later means one new class and one registry entry (SPEC §8.3).

Adapters take a :class:`UpstreamTarget` — a plain frozen record — rather than the ORM
model. That keeps them unit-testable without a database, and keeps the decrypted
credential out of a mapped object that could otherwise be serialised somewhere it should
not go.

The protocol has five methods and they divide along one line: ``prepare`` writes the
provider's dialect, and ``parse``/``parse_stream``/``error`` read it. ``dropped`` is the
odd one out — it answers a question about a dialect without making a call, so the request
log can say which of the caller's parameters this provider cannot carry. None of them
knows about HTTP status codes the client will see, gateways, or policy: an adapter
decides wire format, and everything above it decides what to do about the answer.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.schemas.openai import ChatRequest, ChatResponse, StreamFrame

# Longest upstream error text relayed to the client. A provider behind a misconfigured
# proxy will happily return a full HTML page.
MAX_UPSTREAM_MESSAGE = 1000


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
    #: Tokens the provider will accept in one request, or ``None`` when nobody has said.
    #: Read only by the prompt assembler's overflow guard (SPEC §7); ``None`` disables it,
    #: because a guessed window would drop memory from requests that would have been fine.
    context_window: int | None = None
    #: The tokenizer this model's counts are measured with (task 101), as the registry
    #: key — ``o200k_base``, ``approximate:3.5`` — already resolved from the catalog row
    #: when the gateway payload was built. ``None`` means the process default, which is
    #: what a target built by hand in a test gets.
    tokenizer: str | None = None

    def __repr__(self) -> str:
        # The default dataclass repr would put the decrypted credential into any log line
        # or traceback that happens to include a target.
        return f"UpstreamTarget(id={self.id}, name={self.name!r}, dialect={self.dialect!r})"


@dataclass(frozen=True, slots=True)
class UpstreamError:
    """A provider failure, in the terms the caller's SDK understands.

    Deliberately data rather than an exception: the proxy raises one thing for a failed
    request, the connectivity probe renders another for the same response, and both need
    the *same* four fields. An adapter that raised would force one of them to catch what
    the other wants to display.

    ``status_code`` is the status the client should see, which is not always the one the
    provider sent — Anthropic's 529 means "overloaded, come back", and a client SDK only
    knows to do that if it arrives as a 503.
    """

    status_code: int
    message: str
    type: str | None = None
    code: str | None = None
    param: str | None = None


class MalformedUpstreamResponse(Exception):
    """The upstream returned 2xx with a body that is not a chat completion."""


class UpstreamStreamFailed(Exception):
    """The provider reported a failure *inside* an SSE stream.

    Distinct from a transport error because nothing went wrong with the connection: the
    provider decided, several frames in, that it could not continue. The status line went
    out long ago, so this can only end as a terminating error frame (SPEC §8.2) — which is
    exactly what the proxy does with a transport failure at the same point, and why this
    is caught in the same place.
    """


class DialectRejected(Exception):
    """The request cannot be expressed in this provider's dialect.

    A client error, not an upstream one — nothing has been sent. Raised rather than
    quietly approximated, because the failures worth refusing are the ones where an
    approximation would be *answered*: ``n: 3`` against a provider that returns one
    completion is a request nobody would notice had been ignored until the bill arrived.
    """

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param


class UpstreamAdapter(Protocol):
    """Translate between the gateway's OpenAI-shaped view and one provider's dialect.

    ``request`` is the *outbound* request: prompt layers already applied and parameters
    already merged. An adapter decides wire format and auth, not policy.
    """

    dialect: str

    def prepare(self, request: ChatRequest, target: UpstreamTarget) -> httpx.Request: ...

    def parse(self, response: httpx.Response) -> ChatResponse: ...

    def parse_stream(
        self, response: httpx.Response, request: ChatRequest
    ) -> AsyncIterator[StreamFrame]:
        """The provider's stream as OpenAI chunks.

        Takes the outbound request as well as the response because translating a
        stream can need to know what was asked for: ``stream_options.include_usage``
        changes which frames OpenAI sends, and a dialect that has to *produce* those
        frames rather than relay them has no other way to find out.
        """
        ...

    def error(self, response: httpx.Response) -> UpstreamError: ...

    def dropped(self, params: Collection[str]) -> tuple[str, ...]:
        """Which of these generation parameters this dialect cannot carry.

        Answered without a request in hand, so the request log can record it and the
        model form can warn about it from the same source of truth.
        """
        ...


def error_fields(response: httpx.Response) -> UpstreamError:
    """The default error translation: relay the provider's own status and words.

    Works for every provider that uses the OpenAI error envelope, which — usefully — is
    also the envelope Anthropic uses for ``message`` and ``type``. Preserving all four
    fields is what lets a client SDK raise ``RateLimitError`` for an upstream 429 instead
    of a generic server error, which is the difference between a caller that backs off and
    one that retries immediately.
    """
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    message = error_type = error_code = param = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = _as_text(error.get("message"))
            error_type = _as_text(error.get("type"))
            error_code = _as_text(error.get("code"))
            param = _as_text(error.get("param"))
        elif isinstance(error, str):
            message = error
        else:
            message = _as_text(payload.get("detail")) or _as_text(payload.get("message"))

    if not message:
        message = response.text.strip() or f"returned HTTP {response.status_code}"
    return UpstreamError(
        status_code=response.status_code,
        message=message[:MAX_UPSTREAM_MESSAGE],
        type=error_type,
        code=error_code,
        param=param,
    )


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


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
