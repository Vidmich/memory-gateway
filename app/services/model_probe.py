"""Test connection: does this model configuration actually work?

The button this backs matters more than it looks. Without it, every misconfiguration —
a base URL missing `/v1`, a key pasted with a trailing newline, an Azure deployment name
in the wrong place — surfaces later as a confusing proxy error on somebody else's
request, and the config screen still looks fine.

So the probe goes through the **same adapter and the same headers** the real request path
uses. A probe that built its own request would validate a code path nobody serves traffic
on, and would still say "OK" for a model that 401s in production.

Cost is negligible by construction: one user turn of two characters and ``max_tokens: 1``.
That is a handful of tokens even on the most expensive model, which is what makes it safe
to put behind a button people press repeatedly while they get the URL right.

The result is deliberately *not* an exception. "The upstream said 401 invalid_api_key" is
the useful answer to this question, not a failure of the request that asked it, and the
UI renders the four fields side by side.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Protocol

import httpx

from app.adapters.base import UpstreamTarget, get_adapter
from app.adapters.openai import MalformedUpstreamResponse
from app.schemas.openai import ChatMessage, ChatRequest
from app.services.proxy import MAX_UPSTREAM_MESSAGE, upstream_error_fields

logger = logging.getLogger(__name__)

#: The smallest thing that still exercises the whole path: auth, URL, dialect, and the
#: provider's own model-id lookup. ``model`` is replaced by the adapter with the target's
#: ``upstream_model_id``, so the value here is never sent.
PROBE_REQUEST = ChatRequest(
    model="probe",
    messages=[ChatMessage(role="user", content="hi")],
    max_tokens=1,
    stream=False,
)

#: A probe is a health check, not a completion. Cutting the read budget short keeps a
#: hung provider from occupying a worker for the model's full ``timeout_seconds`` — and a
#: provider that takes longer than this to emit one token is a finding either way.
MAX_PROBE_SECONDS = 20


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the UI renders: green with a latency, or red with the upstream's own words."""

    ok: bool
    latency_ms: int
    #: The provider's HTTP status, when there was one. ``None`` means the request never
    #: got that far — DNS, TLS, connection refused, timeout.
    upstream_status: int | None = None
    error_message: str | None = None
    #: The ``model`` the provider echoed back. Catches the case where a request succeeds
    #: but silently lands on a different deployment than the one that was configured.
    model_echo: str | None = None


class Probe(Protocol):
    """The seam. :class:`CatalogService` depends on this rather than on the class below,
    so a test can assert *what* was sent — including that the stored credential really
    was decrypted and handed over — without a socket."""

    async def run(self, target: UpstreamTarget) -> ProbeResult: ...


class ModelProbe:
    """Sends the probe. Takes the shared HTTP client, like the proxy does."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def run(self, target: UpstreamTarget) -> ProbeResult:
        try:
            adapter = get_adapter(target.dialect)
        except ValueError:
            # Reachable only if a dialect passes the write-time check and then loses its
            # adapter; the create/update path refuses an unregistered one outright.
            return ProbeResult(
                ok=False,
                latency_ms=0,
                error_message=f"This build has no adapter for the '{target.dialect}' dialect.",
            )

        budget = min(target.timeout_seconds, MAX_PROBE_SECONDS)
        request = adapter.prepare(PROBE_REQUEST, _with_timeout(target, budget))

        started = time.perf_counter()
        try:
            response = await self._http.send(request)
        except httpx.TimeoutException:
            return ProbeResult(
                ok=False,
                latency_ms=_elapsed_ms(started),
                error_message=f"No response within {budget}s.",
            )
        except httpx.HTTPError as exc:
            # The verbatim transport error, because "could not connect" alone does not
            # distinguish a typo in the host from a firewall. It names the host, never a
            # header, so the credential cannot appear here.
            return ProbeResult(
                ok=False,
                latency_ms=_elapsed_ms(started),
                error_message=_transport_message(exc),
            )

        latency_ms = _elapsed_ms(started)
        if response.status_code >= 400:
            message, _, code, _ = upstream_error_fields(response)
            return ProbeResult(
                ok=False,
                latency_ms=latency_ms,
                upstream_status=response.status_code,
                # `401 invalid_api_key` reads better than either half alone, and matches
                # what the task's demo promises the button shows.
                error_message=f"{code} {message}".strip() if code else message,
            )

        try:
            completion = adapter.parse(response)
        except MalformedUpstreamResponse as exc:
            return ProbeResult(
                ok=False,
                latency_ms=latency_ms,
                upstream_status=response.status_code,
                error_message=(
                    f"Responded {response.status_code}, but the body is not a chat "
                    f"completion: {str(exc)[:MAX_UPSTREAM_MESSAGE]}"
                ),
            )

        return ProbeResult(
            ok=True,
            latency_ms=latency_ms,
            upstream_status=response.status_code,
            model_echo=completion.model or None,
        )


def _with_timeout(target: UpstreamTarget, seconds: int) -> UpstreamTarget:
    return replace(target, timeout_seconds=seconds)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _transport_message(exc: httpx.HTTPError) -> str:
    detail = str(exc).strip()
    kind = type(exc).__name__
    return (f"{kind}: {detail}" if detail else kind)[:MAX_UPSTREAM_MESSAGE]
