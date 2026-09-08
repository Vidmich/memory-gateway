"""The ``openai`` dialect: passthrough.

Also covers Azure OpenAI, Together, Groq, vLLM, Ollama, and OpenRouter — everything
OpenAI-shaped. The differences between them are base URL and auth style, which is exactly
what :class:`~app.adapters.base.UpstreamTarget` carries.

Nothing is translated here, and that is the point of the dialect: the client's bytes go
out and the provider's bytes come back, so a field neither this code nor this release has
heard of still works.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Collection

import httpx
from pydantic import ValidationError

from app.adapters.base import (
    MalformedUpstreamResponse,
    UpstreamError,
    UpstreamTarget,
    error_fields,
    register,
)
from app.adapters.http import build_headers, endpoint_url, timeouts
from app.schemas.openai import (
    UNSUPPORTED_FIELDS,
    ChatChunk,
    ChatRequest,
    ChatResponse,
    StreamFrame,
)
from app.services.sse import DONE, iter_sse

logger = logging.getLogger(__name__)


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

    async def parse_stream(
        self, response: httpx.Response, request: ChatRequest
    ) -> AsyncIterator[StreamFrame]:
        """``request`` is unused, and that is the dialect: the provider already
        applied whatever the client asked for, and these are its own bytes."""
        async for event in iter_sse(response.aiter_bytes()):
            if not event.data or event.data == DONE:
                # The gateway writes its own terminator once the stream really ends.
                continue
            yield StreamFrame(data=event.data, chunk=_parse_chunk(event.data))

    def error(self, response: httpx.Response) -> UpstreamError:
        return error_fields(response)

    def dropped(self, params: Collection[str]) -> tuple[str, ...]:
        """Nothing. Whatever the client sent went out unchanged, which is the dialect."""
        return ()


def chat_completions_url(base_url: str) -> str:
    return endpoint_url(base_url, "chat/completions")


def _parse_chunk(data: str) -> ChatChunk | None:
    try:
        return ChatChunk.model_validate_json(data)
    except ValidationError:
        # Relayed anyway: the client's SDK is a better judge of the provider's output
        # than this gateway is. Only the gateway's own parsed view is lost.
        logger.debug("stream frame did not parse as a completion chunk")
        return None


adapter = register(OpenAIAdapter())
