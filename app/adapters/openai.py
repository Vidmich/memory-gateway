"""The ``openai`` dialect: passthrough.

Also covers Azure OpenAI, Together, Groq, vLLM, Ollama, and OpenRouter — everything
OpenAI-shaped. The differences between them are base URL and auth style, which is exactly
what :class:`~app.adapters.base.UpstreamTarget` carries.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from app.adapters.base import UpstreamTarget, register
from app.schemas.openai import (
    UNSUPPORTED_FIELDS,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    StreamFrame,
)
from app.services.sse import DONE, iter_sse

logger = logging.getLogger(__name__)

# Used when `auth_type` is api_key_header and the model does not name a header itself.
# Providers that want a different one set it through `extra_headers`, which is applied
# last and therefore wins.
DEFAULT_API_KEY_HEADER = "x-api-key"

# Establishing a TCP+TLS connection should never take as long as generating a completion,
# so the connect budget is fixed and short while the read budget is per-model. httpx
# applies the read timeout to each read, which for a streaming response makes it the
# time-to-first-byte budget and then the gap-between-chunks budget.
CONNECT_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 10.0
POOL_TIMEOUT_SECONDS = 5.0


class OpenAIAdapter:
    dialect = "openai"

    def prepare(self, request: ChatRequest, target: UpstreamTarget) -> httpx.Request:
        payload = request.model_dump(exclude_none=True)
        for name in UNSUPPORTED_FIELDS:
            payload.pop(name, None)
        # The client names a *virtual* model; the upstream gets the real one.
        payload["model"] = target.upstream_model_id
        payload["stream"] = request.stream

        return httpx.Request(
            "POST",
            chat_completions_url(target.base_url),
            headers=build_headers(target, stream=request.stream),
            content=json.dumps(payload).encode("utf-8"),
            extensions={"timeout": timeouts(target.timeout_seconds)},
        )

    def parse(self, response: httpx.Response) -> ChatResponse:
        try:
            return ChatResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise MalformedUpstreamResponse(str(exc)) from exc

    async def parse_stream(self, response: httpx.Response) -> AsyncIterator[StreamFrame]:
        async for event in iter_sse(response.aiter_bytes()):
            if not event.data or event.data == DONE:
                # The gateway writes its own terminator once the stream really ends.
                continue
            yield StreamFrame(data=event.data, chunk=_parse_chunk(event.data))


class MalformedUpstreamResponse(Exception):
    """The upstream returned 2xx with a body that is not a chat completion."""


def timeouts(read_seconds: float) -> dict[str, float]:
    return {
        "connect": CONNECT_TIMEOUT_SECONDS,
        "read": read_seconds,
        "write": WRITE_TIMEOUT_SECONDS,
        "pool": POOL_TIMEOUT_SECONDS,
    }


def build_headers(target: UpstreamTarget, *, stream: bool) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }
    headers.update(_auth_headers(target))
    # Explicit per-model headers are applied last so an operator can override anything
    # above, including the auth header name.
    headers.update({name.lower(): value for name, value in target.extra_headers.items()})
    return headers


def _auth_headers(target: UpstreamTarget) -> dict[str, str]:
    if target.auth_type == "none" or not target.credential:
        return {}
    if target.auth_type == "bearer":
        return {"authorization": f"Bearer {target.credential}"}
    if target.auth_type == "api_key_header":
        return {DEFAULT_API_KEY_HEADER: target.credential}
    if target.auth_type == "azure":
        # Azure OpenAI keys go in `api-key`; the deployment and api-version live in the
        # base URL, which is why the query string is preserved below.
        return {"api-key": target.credential}
    raise ValueError(f"unknown auth_type {target.auth_type!r}")


def chat_completions_url(base_url: str) -> str:
    """Append the endpoint path while keeping any query string on the base URL.

    Azure OpenAI carries ``?api-version=`` on the base URL; naive concatenation would
    produce ``.../chat/completions?api-version=...`` only by accident, and dropping it
    yields a 404 that looks like a wrong deployment name.
    """
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/") + "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _parse_chunk(data: str) -> ChatChunk | None:
    try:
        return ChatChunk.model_validate_json(data)
    except ValidationError:
        # Relayed anyway: the client's SDK is a better judge of the provider's output
        # than this gateway is. Only the gateway's own parsed view is lost.
        logger.debug("stream frame did not parse as a completion chunk")
        return None


adapter = register(OpenAIAdapter())
