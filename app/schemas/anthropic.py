"""Anthropic's Messages API, as far as the gateway reads it.

Only the *inbound* half is modelled. The outbound request is built as a plain dict by
:mod:`app.adapters.anthropic`, because it is an allowlist by construction — a field this
gateway has not been taught is a field Anthropic would reject, so there is nothing to
carry through and nothing to validate.

Reading is the opposite problem, and these models are deliberately forgiving. Every one
sets ``extra="allow"`` and gives every field a default, so a provider that adds a content
block type, a stop reason, or a stream event this build has never seen produces a
translation that ignores it rather than a 502 for the caller. A gateway that breaks when
its provider ships a feature is a gateway nobody can leave in the path.

One model serves two purposes on purpose: :class:`AnthropicMessage` is both the body of a
non-streaming response and the ``message`` inside a ``message_start`` event, because
Anthropic sends the same shape for both — the streaming one simply arrives with empty
content and no stop reason yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_PASSTHROUGH = ConfigDict(extra="allow")


class AnthropicUsage(BaseModel):
    """Token counts. Both default to zero because a ``message_delta`` carries only the
    output half, and adding a missing count to a running total is what the translation
    does with it."""

    model_config = _PASSTHROUGH

    input_tokens: int = 0
    output_tokens: int = 0


class ContentBlock(BaseModel):
    """One block of a response. ``text`` is null for every type but ``text``, which is
    the only one this build renders — tool use and extended thinking are SPEC §16
    items, and a block whose text is null contributes nothing to the completion."""

    model_config = _PASSTHROUGH

    type: str = "text"
    text: str | None = None


class AnthropicMessage(BaseModel):
    model_config = _PASSTHROUGH

    id: str = ""
    type: str = "message"
    role: str = "assistant"
    model: str = ""
    content: list[ContentBlock] = Field(default_factory=list)
    #: Null while a stream is still running, which is why the translation cannot read a
    #: finish reason before ``message_delta``.
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)

    def text(self) -> str:
        """The completion, as OpenAI would carry it: text blocks concatenated in order."""
        return "".join(block.text for block in self.content if block.text)


class EventDelta(BaseModel):
    """The ``delta`` of a stream event.

    Two different events use the key for two different things — ``content_block_delta``
    puts generated text in it, ``message_delta`` puts the stop reason in it — so this
    carries both and the translation reads whichever the event type means.
    """

    model_config = _PASSTHROUGH

    #: ``text_delta``, or ``input_json_delta``/``thinking_delta`` for features this build
    #: does not render. Checked rather than assumed, so a thinking block never leaks into
    #: a client's content stream as if the model had said it out loud.
    type: str | None = None
    text: str | None = None
    stop_reason: str | None = None
    stop_sequence: str | None = None


class AnthropicErrorBody(BaseModel):
    model_config = _PASSTHROUGH

    type: str = ""
    message: str = ""


class StreamEvent(BaseModel):
    """One SSE payload from a Messages stream, whichever kind it is.

    A single model rather than a union keyed on ``type``, because the translation is a
    match on ``type`` anyway and a union would turn an unrecognised event — the thing
    that must be *ignored* — into a validation error.
    """

    model_config = _PASSTHROUGH

    type: str = ""
    index: int = 0
    #: ``message_start``.
    message: AnthropicMessage | None = None
    #: ``content_block_start``. Non-empty text here is an assistant prefill being echoed.
    content_block: ContentBlock | None = None
    #: ``content_block_delta`` and ``message_delta``.
    delta: EventDelta | None = None
    #: ``message_delta``, and cumulative rather than incremental.
    usage: AnthropicUsage | None = None
    #: ``error``.
    error: AnthropicErrorBody | None = None


__all__ = [
    "AnthropicErrorBody",
    "AnthropicMessage",
    "AnthropicUsage",
    "ContentBlock",
    "EventDelta",
    "StreamEvent",
]
