"""``POST /gateways/{id}/test`` — a probe completion through the real proxy path.

Monitoring answers this for traffic that has already happened; this answers it for a
change nobody has sent a request through yet — "what is this gateway
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

It also walks the whole routing chain, which matters more than it sounds. A probe that
only tried the primary would report a broken primary as a failure on a gateway that in
fact serves every request perfectly; a probe that failed over silently would report a
green tick on a gateway whose primary is dead. So it does what a request does and reports
every attempt, and the editor renders "answered by the secondary, after the primary
returned 503" — which is the sentence somebody actually needs.

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
from app.services.proxy import Prepared
from app.services.routing import Attempt, Attempts, Router, plan

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
    #: Every target this probe tried, in order. Empty for a gateway with one target, for
    #: the same reason the log column is: one attempt is fully described by the fields
    #: above it.
    attempts: tuple[Attempt, ...] = ()


class GatewayProbe(Protocol):
    async def run(self, slug: str, *, message: str) -> GatewayProbeResult: ...


class ProxyGatewayProbe:
    """The real thing: resolver plus router, exactly as the data plane wires them."""

    def __init__(self, resolver: GatewayResolver, router: Router) -> None:
        self._resolver = resolver
        self._router = router

    async def run(self, slug: str, *, message: str) -> GatewayProbeResult:
        started = time.perf_counter()
        request = ChatRequest(
            model=slug,
            messages=[ChatMessage(role="user", content=message[:MAX_PROBE_MESSAGE])],
            max_tokens=PROBE_MAX_TOKENS,
            stream=False,
        )

        # The last prompt actually assembled, captured as the chain walks. On a failure
        # this is what shows the operator what went upstream — which is most of why they
        # pressed the button.
        seen: list[Prepared] = []
        attempts = Attempts(on_prepared=seen.append)

        try:
            gateway = await self._resolver.resolve(slug)
            routing = plan(gateway)
        except ProxyError as exc:
            # A disabled gateway, a switched-off model, a credential that will not
            # decrypt. All of them are answers, and all of them name the fix.
            return _failed(started, exc)

        upstream_started = time.perf_counter()
        try:
            completed = await self._router.complete(request, gateway, routing, attempts)
        except ProxyError as exc:
            last = seen[-1] if seen else None
            return _failed(
                started,
                exc,
                prompt=_prompt_of(last.request) if last else (),
                model=last.target.name if last else None,
                attempts=tuple(attempts.records),
            )

        upstream_ms = _elapsed(upstream_started)
        return GatewayProbeResult(
            ok=True,
            total_ms=_elapsed(started),
            upstream_ms=upstream_ms,
            assembled_prompt=_prompt_of(completed.prepared.request),
            model_name=completed.prepared.target.name,
            content=_first_choice(completed.response),
            locked_overrides=completed.prepared.params.overridden,
            # Only when something was tried and rejected. One clean attempt needs no
            # timeline, and rendering one would make every gateway look like a chain.
            attempts=tuple(attempts.records) if len(attempts) > 1 else (),
        )


def _failed(
    started: float,
    exc: ProxyError,
    *,
    prompt: tuple[PromptMessage, ...] = (),
    model: str | None = None,
    attempts: tuple[Attempt, ...] = (),
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
        # Every attempt, including on failure — "all three targets returned 503" is a
        # different problem from "the one target returned 503", and only the list says
        # which.
        attempts=attempts,
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
