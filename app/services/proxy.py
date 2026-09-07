"""Forwarding a chat completion to an upstream model.

Two shapes, one pipeline. The non-streaming path is a request/response; the streaming path
splits into "send and check the status" and "relay frames", because once the first byte of
an SSE body has been written the HTTP status is already on the wire and an error can no
longer be reported as one. Validating before returning the body iterator is what makes a
provider 429 come back to the client as a 429.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.adapters.base import UpstreamAdapter, UpstreamTarget, get_adapter
from app.adapters.openai import MalformedUpstreamResponse
from app.api.proxy.errors import (
    GatewayUnavailable,
    UpstreamStatus,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from app.schemas.openai import ChatRequest, ChatResponse
from app.services.gateway_resolver import ResolvedGateway
from app.services.params import Resolved, resolve_params
from app.services.prompt import PromptAssembler, PromptLayer
from app.services.sse import DONE, format_event

logger = logging.getLogger(__name__)

# Longest upstream error text relayed to the client. A provider behind a misconfigured
# proxy will happily return a full HTML page.
MAX_UPSTREAM_MESSAGE = 1000


@dataclass(frozen=True, slots=True)
class Prepared:
    """A request that has been through prompt assembly and the parameter merge.

    Carries the merge result alongside the payload because the two are read at different
    moments: the request goes upstream, the ``overridden`` list goes into a response
    header that has to be written before any body.
    """

    request: ChatRequest
    params: Resolved
    target: UpstreamTarget


class ProxyService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        assembler: PromptAssembler | None = None,
    ) -> None:
        self._http = http
        self._assembler = assembler or PromptAssembler()

    # -- request construction ------------------------------------------------

    def prepare(
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        target: UpstreamTarget,
    ) -> Prepared:
        """Apply the prompt layers and the parameter merge, once.

        The result is still a ``ChatRequest``: the adapter's job starts at wire format,
        not at policy, and a future ``tools`` field threads through here untouched. It is
        a separate step from sending because the caller needs the merge *before* the
        response exists — the locked-parameter header goes out with the status line, and
        on a stream that is before the first token.
        """
        messages = self._assembler.assemble(
            request.messages,
            [
                PromptLayer("model.system_context", target.system_context),
                PromptLayer("gateway.system_context", gateway.system_context),
                # Tasks 10 and 12 add the document and end-user memory layers here.
            ],
        )
        params = resolve_params(
            model_defaults=target.default_params,
            gateway_overrides=gateway.param_overrides,
            client=request.client_parameters(),
            locked=gateway.locked_params,
        )

        payload: dict[str, Any] = request.model_dump(exclude_none=True)
        payload.update(params.values)
        payload["messages"] = [message.model_dump(exclude_none=True) for message in messages]
        return Prepared(request=ChatRequest.model_validate(payload), params=params, target=target)

    # -- non-streaming -------------------------------------------------------

    async def complete(self, prepared: Prepared) -> ChatResponse:
        target = prepared.target
        adapter = _adapter_for(target)
        outbound = adapter.prepare(prepared.request, target)

        try:
            response = await self._http.send(outbound)
        except httpx.TimeoutException as exc:
            raise _timeout(target, exc) from exc
        except httpx.HTTPError as exc:
            raise _unreachable(target, exc) from exc

        # A non-streaming send has already read and closed the body.
        _raise_for_upstream_status(response, target)
        try:
            return adapter.parse(response)
        except MalformedUpstreamResponse as exc:
            logger.warning(
                "upstream returned an unparseable completion",
                extra={"model": target.name, "error": str(exc)},
            )
            raise GatewayUnavailable(
                f"[upstream:{target.name}] returned a response that is not a chat completion."
            ) from exc

    # -- streaming -----------------------------------------------------------

    async def open_stream(self, prepared: Prepared) -> UpstreamStream:
        """Start the upstream call and validate its status. Nothing is yielded yet."""
        target = prepared.target
        adapter = _adapter_for(target)
        outbound = adapter.prepare(prepared.request, target)

        try:
            response = await self._http.send(outbound, stream=True)
        except httpx.TimeoutException as exc:
            raise _timeout(target, exc) from exc
        except httpx.HTTPError as exc:
            raise _unreachable(target, exc) from exc

        if response.status_code >= 400:
            try:
                await response.aread()
                _raise_for_upstream_status(response, target)
            finally:
                await response.aclose()

        return UpstreamStream(response=response, adapter=adapter, target=target)


class UpstreamStream:
    """Relays SSE frames downstream as they arrive, with no buffering in between."""

    def __init__(
        self,
        *,
        response: httpx.Response,
        adapter: UpstreamAdapter,
        target: UpstreamTarget,
    ) -> None:
        self._response = response
        self._adapter = adapter
        self.target = target

    async def frames(self) -> AsyncIterator[str]:
        try:
            async for frame in self._adapter.parse_stream(self._response):
                yield format_event(frame.data)
            yield format_event(DONE)
        except asyncio.CancelledError:
            # The client hung up. Closing the response below cancels the upstream call so
            # the provider stops generating — and stops charging for — tokens nobody will
            # read.
            logger.info(
                "client disconnected mid-stream; cancelling upstream",
                extra={"model": self.target.name},
            )
            raise
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            # The status line went out with the first frame, so this cannot become a 504.
            # SPEC §8.2: terminate the stream with an error event instead.
            logger.warning(
                "upstream stream failed after it had started",
                extra={"model": self.target.name, "error": type(exc).__name__},
            )
            yield format_event(_stream_error(self.target, exc))
        finally:
            await self._response.aclose()


def _adapter_for(target: UpstreamTarget) -> UpstreamAdapter:
    try:
        return get_adapter(target.dialect)
    except ValueError as exc:
        raise GatewayUnavailable(
            f"Upstream model '{target.name}' uses dialect '{target.dialect}', "
            f"which this build does not support."
        ) from exc


def _timeout(target: UpstreamTarget, exc: Exception) -> UpstreamTimeout:
    logger.warning(
        "upstream timed out",
        extra={"model": target.name, "timeout_seconds": target.timeout_seconds},
    )
    return UpstreamTimeout(
        f"[upstream:{target.name}] did not respond within {target.timeout_seconds}s."
    )


def _unreachable(target: UpstreamTarget, exc: Exception) -> UpstreamUnavailable:
    # `str(exc)` on a transport error can contain the full URL; the model name is enough
    # for the caller, and the detail goes to the log.
    logger.warning(
        "upstream connection failed",
        extra={"model": target.name, "error": type(exc).__name__},
    )
    return UpstreamUnavailable(f"[upstream:{target.name}] could not be reached.")


def _raise_for_upstream_status(response: httpx.Response, target: UpstreamTarget) -> None:
    """Relay a provider error with its own status, type and code.

    Preserving them is what lets a client SDK raise ``RateLimitError`` for an upstream 429
    instead of a generic server error — which is the difference between a caller that
    backs off and one that retries immediately.
    """
    if response.status_code < 400:
        return

    message, error_type, error_code, param = upstream_error_fields(response)
    raise UpstreamStatus(
        status_code=response.status_code,
        model_name=target.name,
        message=message,
        upstream_type=error_type,
        upstream_code=error_code,
        param=param,
    )


def upstream_error_fields(
    response: httpx.Response,
) -> tuple[str, str | None, str | None, str | None]:
    """``(message, type, code, param)`` dug out of whatever the provider returned.

    Public because the connectivity probe (:mod:`app.services.model_probe`) has to report
    the same text the proxy would have relayed. If the two ever disagreed, "Test
    connection" would say something the live request does not.
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
    return message[:MAX_UPSTREAM_MESSAGE], error_type, error_code, param


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _stream_error(target: UpstreamTarget, exc: Exception) -> str:
    kind = "upstream_timeout" if isinstance(exc, httpx.TimeoutException) else "upstream_error"
    return json.dumps(
        {
            "error": {
                "message": f"[upstream:{target.name}] the stream ended before it completed.",
                "type": "server_error",
                "param": None,
                "code": kind,
            }
        }
    )
