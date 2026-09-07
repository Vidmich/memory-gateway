"""The OpenAI chat-completions wire format.

Every model here sets ``extra="allow"``. The gateway is a proxy, not a validator: a field
this code has never heard of belongs to the upstream provider, and rejecting it would
break clients whenever OpenAI ships something new. The only fields refused are the ones
in :data:`UNSUPPORTED_FIELDS`, which the gateway cannot honour and must not silently drop
(SPEC §12.1).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_PASSTHROUGH = ConfigDict(extra="allow")

# Fields the gateway does not implement. Sending one is an error naming it, because a
# silently dropped `tools` produces an agent that narrates tool calls as prose — a very
# expensive failure to diagnose from the outside. Tool passthrough is the first post-v1
# item (SPEC §16.1).
UNSUPPORTED_FIELDS = ("tools", "tool_choice", "functions", "function_call", "logprobs")

# Generation parameters subject to the merge in ``app.services.params``. Anything else the
# client sends rides along untouched.
PARAMETER_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
)


class ChatMessage(BaseModel):
    """One message. ``content`` may be a string or the multi-part array form."""

    model_config = _PASSTHROUGH

    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model_config = _PASSTHROUGH

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    # The OpenAI convention for an end-user identifier; task 12 resolves memory from it.
    user: str | None = None

    # Declared so they can be named in a 400 rather than swept into `extra`.
    tools: Any | None = None
    tool_choice: Any | None = None
    functions: Any | None = None
    function_call: Any | None = None
    logprobs: Any | None = None

    def unsupported_field(self) -> str | None:
        """The first unsupported field the client actually asked for, if any.

        ``logprobs: false`` and ``tools: []`` request nothing, so they are not errors —
        only a value that would change the response is refused.
        """
        for name in UNSUPPORTED_FIELDS:
            value = getattr(self, name, None)
            if value is None or value is False or value == [] or value == {}:
                continue
            return name
        return None

    def client_parameters(self) -> dict[str, Any]:
        """Generation parameters this request explicitly set."""
        return {
            name: getattr(self, name)
            for name in PARAMETER_FIELDS
            if name in self.model_fields_set and getattr(self, name) is not None
        }


class Usage(BaseModel):
    model_config = _PASSTHROUGH

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ResponseMessage(BaseModel):
    model_config = _PASSTHROUGH

    role: str = "assistant"
    content: str | None = None


class Choice(BaseModel):
    model_config = _PASSTHROUGH

    index: int = 0
    message: ResponseMessage | None = None
    finish_reason: str | None = None


class ChatResponse(BaseModel):
    model_config = _PASSTHROUGH

    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice] = Field(default_factory=list)
    usage: Usage | None = None


class ChunkDelta(BaseModel):
    model_config = _PASSTHROUGH

    role: str | None = None
    content: str | None = None


class ChunkChoice(BaseModel):
    model_config = _PASSTHROUGH

    index: int = 0
    delta: ChunkDelta = Field(default_factory=ChunkDelta)
    finish_reason: str | None = None


class ChatChunk(BaseModel):
    model_config = _PASSTHROUGH

    id: str = ""
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: list[ChunkChoice] = Field(default_factory=list)
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class StreamFrame:
    """One downstream SSE payload, plus the parsed view of it when it parsed.

    ``data`` is what gets written after ``data: ``. For an OpenAI-dialect upstream it is
    the provider's own bytes, verbatim — no re-serialisation, so nothing this code has
    never heard of can be lost on the way through. A translating dialect (task 16) fills
    it from ``chunk`` instead. ``chunk`` is ``None`` when the frame did not parse as a
    completion chunk; the frame is still relayed, because the client's SDK is a better
    judge of the provider's output than the gateway is.
    """

    data: str
    chunk: ChatChunk | None = None


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "memory-gateway"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard] = Field(default_factory=list)
