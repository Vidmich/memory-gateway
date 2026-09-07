"""``POST /gateways/{id}/test`` — a probe completion through the real proxy path.

Before task 07's monitoring exists, this is the only way to answer "what is this gateway
actually sending?" without reading the code. So it is deliberately *not* a simplified
re-implementation: it goes through the same resolver the data plane uses (cache included),
the same prompt assembly, the same parameter merge and the same adapter. If the button is
green and a real request is not, the two paths have diverged, and the whole point is that
they cannot.

The two things it does differently from a customer call are both about who is asking. It
takes no API key — the caller is already authenticated on the control plane, and issuing a
key to test a gateway would be a strange requirement — and it returns the **assembled
prompt**, which a data-plane response never does, because seeing what the system context
turned into is most of the value.

Like :class:`~app.services.model_probe.ModelProbe`, a failure is a *result*, not an
exception: "the upstream said 401" is the successful answer to "does this work".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from app.api.proxy.errors import ProxyError, UpstreamStatus
from app.schemas.openai import ChatMessage, ChatRequest, ChatResponse
from app.services.gateway_resolver import GatewayResolver
from app.services.prompt import as_text
from app.services.proxy import ProxyService

logger = logging.getLogger(__name__)

#: What a probe costs. Small enough to press repeatedly, large enough that the answer is
#: a real completion rather than an empty one that hides a broken prompt.
PROBE_MAX_TOKENS = 64
MAX_PROBE_MESSAGE = 2000
DEFAULT_MESSAGE = "Hello! Reply with one short sentence."


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """One message of the assembled prompt, as it goes on the wire."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class GatewayProbeResult:
    ok: bool
    #: Wall clock for the whole probe, and for the upstream call alone. The gap is what
    #: this gateway costs; tasks 10 and 12 add retrieval to the same breakdown.
    total_ms: int
    upstream_ms: int
    #: What was actually sent — the system context, the client turn, and from task 10 the
    #: injected memory.
    assembled_prompt: tuple[PromptMessage, ...] = ()
    model_name: str | None = None
    content: str | None = None
    upstream_status: int | None = None
    error_message: str | None = None
    #: Locked parameters that replaced a value the probe asked for. Empty here in
    #: practice — the probe sends only ``max_tokens`` — but carried so the editor can
    #: show the same explanation the response header gives a customer.
    locked_overrides: tuple[str, ...] = ()


class GatewayProbe(Protocol):
    async def run(self, slug: str, *, message: str) -> GatewayProbeResult: ...


class ProxyGatewayProbe:
    """The real thing: resolver plus proxy, exactly as the data plane wires them."""

    def __init__(self, resolver: GatewayResolver, proxy: ProxyService) -> None:
        self._resolver = resolver
        self._proxy = proxy

    async def run(self, slug: str, *, message: str) -> GatewayProbeResult:
        started = time.perf_counter()
        request = ChatRequest(
            model=slug,
            messages=[ChatMessage(role="user", content=message[:MAX_PROBE_MESSAGE])],
            max_tokens=PROBE_MAX_TOKENS,
            stream=False,
        )

        try:
            gateway = await self._resolver.resolve(slug)
            target = gateway.target()
            prepared = self._proxy.prepare(request, gateway, target)
        except ProxyError as exc:
            # A disabled gateway, a switched-off model, a credential that will not
            # decrypt. All of them are answers, and all of them name the fix.
            return _failed(started, exc)

        upstream_started = time.perf_counter()
        try:
            completion = await self._proxy.complete(prepared)
        except ProxyError as exc:
            return _failed(started, exc, prompt=_prompt_of(prepared.request), model=target.name)

        upstream_ms = _elapsed(upstream_started)
        return GatewayProbeResult(
            ok=True,
            total_ms=_elapsed(started),
            upstream_ms=upstream_ms,
            assembled_prompt=_prompt_of(prepared.request),
            model_name=target.name,
            content=_first_choice(completion),
            locked_overrides=prepared.params.overridden,
        )


def _failed(
    started: float,
    exc: ProxyError,
    *,
    prompt: tuple[PromptMessage, ...] = (),
    model: str | None = None,
) -> GatewayProbeResult:
    return GatewayProbeResult(
        ok=False,
        total_ms=_elapsed(started),
        upstream_ms=0,
        assembled_prompt=prompt,
        model_name=model,
        # The provider's own status when there is one, so "401 from OpenAI" and "503
        # from this gateway" are not the same red box.
        upstream_status=exc.status_code if isinstance(exc, UpstreamStatus) else None,
        error_message=exc.message,
    )


def _prompt_of(outbound: ChatRequest) -> tuple[PromptMessage, ...]:
    return tuple(
        PromptMessage(role=message.role, content=as_text(message.content))
        for message in outbound.messages
    )


def _first_choice(completion: ChatResponse) -> str | None:
    if not completion.choices:
        return None
    message = completion.choices[0].message
    return as_text(message.content if message else None) or None


def _elapsed(since: float) -> int:
    return max(0, round((time.perf_counter() - since) * 1000))


__all__ = [
    "DEFAULT_MESSAGE",
    "MAX_PROBE_MESSAGE",
    "GatewayProbe",
    "GatewayProbeResult",
    "PromptMessage",
    "ProxyGatewayProbe",
]
