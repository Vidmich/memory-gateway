"""The ``anthropic`` dialect: outbound translation to the Messages API (SPEC §8.3).

The client never changes. Everything that makes Claude different from an OpenAI-shaped
provider is absorbed here, and the four differences that actually bite are worth naming
before the code says them again in detail.

**System messages are a parameter, not a message.** Anthropic takes ``system`` at the top
level and rejects — or worse, silently demotes — a system entry in ``messages``. Task 07's
prompt assembler puts the gateway's context, the model's context and the client's own
system turn in that list, so a dialect that dropped them would quietly serve every request
through this gateway without its configured behaviour, and nothing on any screen would
say so. All of them are lifted, in order.

**The message list has structural rules.** Roles must alternate, the first turn must be
``user``, and a trailing assistant turn may not end in whitespace. A client with a history
that breaks any of these is not doing anything wrong by OpenAI's rules, so every one of
them is repaired rather than refused — see :func:`to_turns`. Refusing would make the
gateway's compatibility claim false for the most ordinary case there is: replaying a
stored conversation.

**``max_tokens`` is required.** OpenAI treats it as optional and defaults to the model's
remaining context; Anthropic 400s without it. :func:`max_tokens_for` always produces one,
and the fallback is deliberate rather than unbounded.

**Most OpenAI generation parameters have no equivalent.** They are dropped, and the set is
recorded on the request log (:mod:`app.services.request_log`) so "why did
``presence_penalty`` do nothing" is a thirty-second question rather than a support ticket.
``n > 1`` is the exception: it cannot be dropped quietly, because a caller asking for three
completions and silently receiving one would not notice until it mattered.

Not here, and noted for whoever adds them: **Bedrock and Vertex** speak this same body but
differ in three ways this file assumes away — the model id moves into the URL path
(``/model/{id}/invoke``) rather than the payload, auth is SigV4 or a Google OAuth bearer
rather than ``x-api-key``, and ``anthropic_version`` moves into the *body* as
``bedrock-2023-05-31``. Everything below :func:`translate_request` is reusable for them;
:meth:`AnthropicAdapter.prepare` is not.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Collection, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import httpx
from pydantic import ValidationError

from app.adapters.base import (
    DialectRejected,
    MalformedUpstreamResponse,
    UpstreamError,
    UpstreamStreamFailed,
    UpstreamTarget,
    error_fields,
    register,
)
from app.adapters.http import build_headers, endpoint_url, timeouts
from app.core.ids import uuid7
from app.schemas.anthropic import AnthropicMessage, StreamEvent
from app.schemas.openai import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Choice,
    ResponseMessage,
    StreamFrame,
    Usage,
)
from app.services.sse import iter_sse

logger = logging.getLogger(__name__)

#: The Messages API version header. Anthropic requires it on every request and uses it to
#: keep old integrations working, so pinning a known-good value is the point — an operator
#: who needs a newer one sets ``anthropic-version`` in the model's ``extra_headers``,
#: which is applied last and wins.
API_VERSION = "2023-06-01"
API_VERSION_HEADER = "anthropic-version"

#: Used when neither the request nor the model's ``default_params`` names a limit. Every
#: outbound request carries one, so this is the difference between a working model and a
#: 400 on the first call. Chosen to be generous enough for ordinary chat and small enough
#: that a runaway generation on a misconfigured model is not an open-ended bill.
DEFAULT_MAX_TOKENS = 4096

#: OpenAI's ``temperature`` runs to 2, Anthropic's to 1. Clamped rather than refused: a
#: caller asking for 1.5 wants "more varied", and the closest thing this provider offers
#: is a better answer than a 400 they cannot act on.
MAX_TEMPERATURE = 1.0

#: The smallest non-empty user turn Anthropic will accept, used when a client's history
#: begins with an assistant message. Anything longer would be putting words in the
#: caller's mouth, and this way the model sees a conversation that starts, structurally,
#: where its own first turn does.
LEADING_TURN = "."

#: How the assembler joins system layers, mirrored so a lifted ``system`` parameter reads
#: exactly as the concatenated system message would have.
JOIN = "\n\n"

#: Roles OpenAI uses for instructions to the model. ``developer`` is the newer name for
#: ``system`` and means the same thing here.
SYSTEM_ROLES = frozenset({"system", "developer"})

#: OpenAI generation parameters with no Anthropic equivalent. Dropped from the outbound
#: request and recorded on the log row, because a silent drop is discoverable only by
#: experiment. ``n`` is in the list for the ``n: 1`` case, which is a no-op; ``n > 1`` is
#: refused in :func:`translate_request` instead.
DROPPED_PARAMS: tuple[str, ...] = (
    "presence_penalty",
    "frequency_penalty",
    "n",
    "seed",
    "logit_bias",
    "response_format",
)

#: SPEC §8.3. ``tool_use`` is unreachable until tool passthrough exists (SPEC §16.1) and
#: is mapped now so that the day it becomes reachable is not also the day somebody
#: discovers this table was incomplete. ``pause_turn`` and ``refusal`` are Anthropic's
#: own additions since; both mean the generation ended, which is what ``stop`` says.
STOP_REASONS: Mapping[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "stop",
}

#: Anthropic's error types in OpenAI's vocabulary. The *status* is what a client SDK keys
#: its exception class on, so these are mostly for the human reading the body — except
#: ``overloaded_error``, which changes both.
ERROR_TYPES: Mapping[str, str] = {
    "invalid_request_error": "invalid_request_error",
    "authentication_error": "authentication_error",
    "permission_error": "invalid_request_error",
    "not_found_error": "invalid_request_error",
    "request_too_large": "invalid_request_error",
    "rate_limit_error": "rate_limit_error",
    "billing_error": "invalid_request_error",
    "timeout_error": "api_error",
    "api_error": "api_error",
    "overloaded_error": "api_error",
}

OVERLOADED = "overloaded_error"
#: Anthropic's own status for "overloaded". Nothing in SPEC §8.2's retry table knows what
#: to do with a 529, and treating an unrecognised status as final is what that table does
#: — so this becomes a 503, which is retryable, which is the correct behaviour for a
#: provider asking to be tried again.
OVERLOADED_STATUS = 529
OVERLOADED_AS = 503


class AnthropicAdapter:
    dialect = "anthropic"

    def prepare(self, request: ChatRequest, target: UpstreamTarget) -> httpx.Request:
        payload = translate_request(request, target)
        return httpx.Request(
            "POST",
            messages_url(target.base_url),
            headers=build_headers(
                target,
                stream=request.stream,
                dialect_headers={API_VERSION_HEADER: API_VERSION},
            ),
            content=json.dumps(payload).encode("utf-8"),
            extensions={"timeout": timeouts(target.timeout_seconds)},
        )

    def parse(self, response: httpx.Response) -> ChatResponse:
        try:
            message = AnthropicMessage.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise MalformedUpstreamResponse(str(exc)) from exc
        return to_completion(message)

    async def parse_stream(
        self, response: httpx.Response, request: ChatRequest
    ) -> AsyncIterator[StreamFrame]:
        translator = StreamTranslator(include_usage=wants_usage(request))
        async for event in iter_sse(response.aiter_bytes()):
            if not event.data:
                continue
            for frame in translator.feed(event.data):
                yield frame

    def error(self, response: httpx.Response) -> UpstreamError:
        found = error_fields(response)
        upstream_type = found.type or ""
        overloaded = upstream_type == OVERLOADED or response.status_code == OVERLOADED_STATUS
        return replace(
            found,
            status_code=OVERLOADED_AS if overloaded else found.status_code,
            type=ERROR_TYPES.get(upstream_type, found.type),
            # Anthropic has no `code`, so its error *type* is the most specific thing
            # there is to put here — and `overloaded_error` under a 503 is exactly the
            # detail a 503 alone would have lost.
            code=found.code or found.type,
        )

    def dropped(self, params: Collection[str]) -> tuple[str, ...]:
        return tuple(name for name in DROPPED_PARAMS if name in params)


def messages_url(base_url: str) -> str:
    return endpoint_url(base_url, "messages")


# ---------------------------------------------------------------------------
# request translation
# ---------------------------------------------------------------------------


def translate_request(request: ChatRequest, target: UpstreamTarget) -> dict[str, Any]:
    """The outbound body, built as an allowlist.

    Deliberately *not* the passthrough the OpenAI adapter does. Anthropic rejects a body
    field it does not recognise, so forwarding whatever the client happened to send would
    turn every new OpenAI parameter into a 400 from the provider — the opposite of the
    compatibility this dialect exists to provide.
    """
    if request.n is not None and request.n > 1:
        raise DialectRejected(
            f"This model speaks the Anthropic dialect, which returns one completion per "
            f"request. 'n' must be 1 or omitted; {request.n} was requested.",
            param="n",
        )

    system, turns = split_system(request.messages)
    payload: dict[str, Any] = {
        "model": target.upstream_model_id,
        "messages": to_turns(turns),
        "max_tokens": max_tokens_for(request, target),
        "stream": request.stream,
    }
    if system:
        payload["system"] = system
    if request.temperature is not None:
        payload["temperature"] = clamp_temperature(request.temperature)
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if sequences := stop_sequences(request.stop):
        payload["stop_sequences"] = sequences
    return payload


def split_system(messages: Sequence[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    """Lift every system message out of the list, in order.

    Every one, and from wherever it sits — not just a leading run. A client that sends a
    system message halfway through a conversation is doing something OpenAI allows, and
    the only two alternatives to lifting it are dropping it or letting the provider reject
    the request.
    """
    system: list[str] = []
    rest: list[ChatMessage] = []
    for message in messages:
        if message.role in SYSTEM_ROLES:
            if text := text_of(message.content).strip():
                system.append(text)
        else:
            rest.append(message)
    return JOIN.join(system), rest


def to_turns(messages: Iterable[ChatMessage]) -> list[dict[str, Any]]:
    """The ``messages`` array, repaired into the shape Anthropic will accept.

    Four repairs, all of them for histories OpenAI accepts without comment:

    * consecutive same-role messages are merged, joined the way the assembler joins;
    * empty turns are dropped, because an empty text block is itself a 400;
    * a leading assistant turn gets a minimal user turn in front of it;
    * a trailing assistant turn is right-stripped — Anthropic reads one as a prefill to
      continue from, and refuses to continue from whitespace.

    A role that is neither ``user`` nor ``assistant`` — a tool result, say — becomes a
    user turn. Tools are refused at the route (SPEC §16.1), so this is only reachable for
    a client that hand-builds one, and context from the caller is what it is.
    """
    turns: list[dict[str, Any]] = []
    for message in messages:
        text = text_of(message.content)
        if not text.strip():
            continue
        role = "assistant" if message.role == "assistant" else "user"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"][0]["text"] += JOIN + text
        else:
            turns.append({"role": role, "content": [{"type": "text", "text": text}]})

    if not turns:
        # Every message was empty, or there were none. The route guarantees at least one
        # message, so this is a client sending blanks; a request with no turns is a 400
        # from the provider, and one minimal turn is an answerable question.
        return [{"role": "user", "content": [{"type": "text", "text": LEADING_TURN}]}]

    if turns[0]["role"] == "assistant":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": LEADING_TURN}]})

    if turns[-1]["role"] == "assistant":
        block = turns[-1]["content"][0]
        block["text"] = block["text"].rstrip()

    return turns


def text_of(content: str | list[dict[str, Any]] | None) -> str:
    """The text of a message, whichever of OpenAI's two content shapes it uses.

    The multi-part form is read for its text parts and nothing else: images and audio are
    SPEC §16 items, and silently sending an image part as prose would be worse than
    leaving it out of a request the caller can see returned no description.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, Mapping) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def max_tokens_for(request: ChatRequest, target: UpstreamTarget) -> int:
    """Always a number, in the order the layers mean.

    The request comes first because :func:`app.services.params.resolve_params` has already
    merged the model's defaults into it; ``default_params`` is read directly as well for
    the paths that skip the merge — the connectivity probe is the one that exists today.
    """
    candidates = (
        request.max_tokens,
        request.max_completion_tokens,
        target.default_params.get("max_tokens"),
        target.default_params.get("max_completion_tokens"),
    )
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return DEFAULT_MAX_TOKENS


def clamp_temperature(value: float) -> float:
    return min(max(value, 0.0), MAX_TEMPERATURE)


def stop_sequences(stop: str | list[str] | None) -> list[str]:
    if stop is None:
        return []
    sequences = [stop] if isinstance(stop, str) else list(stop)
    return [text for text in sequences if isinstance(text, str) and text]


# ---------------------------------------------------------------------------
# response translation
# ---------------------------------------------------------------------------


def to_completion(message: AnthropicMessage) -> ChatResponse:
    """One Anthropic message as one OpenAI ``chat.completion``."""
    usage = message.usage
    return ChatResponse(
        id=completion_id(message.id),
        object="chat.completion",
        created=int(time.time()),
        model=message.model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(role="assistant", content=message.text()),
                finish_reason=finish_reason(message.stop_reason),
            )
        ],
        usage=Usage(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            # Computed rather than read: Anthropic does not send a total, and a client
            # reading `total_tokens` for a cost estimate must not get a zero.
            total_tokens=usage.input_tokens + usage.output_tokens,
        ),
    )


def completion_id(message_id: str) -> str:
    """An OpenAI-shaped id that still names the provider's own message.

    Clients match on the ``chatcmpl-`` prefix, and a support conversation that starts with
    a response id should be able to end at Anthropic's own logs without a lookup table.
    """
    return f"chatcmpl-{message_id}" if message_id else f"chatcmpl-{uuid7().hex}"


def finish_reason(stop_reason: str | None) -> str | None:
    """``None`` only while the generation is still running.

    A stop reason this build has not been taught becomes ``stop``: the generation *did*
    end, and a null ``finish_reason`` on a final chunk means "still going" to more than
    one client library.
    """
    if stop_reason is None:
        return None
    mapped = STOP_REASONS.get(stop_reason)
    if mapped is None:
        logger.info("unmapped anthropic stop_reason", extra={"stop_reason": stop_reason})
        return "stop"
    return mapped


# ---------------------------------------------------------------------------
# stream translation
# ---------------------------------------------------------------------------


class StreamTranslator:
    """Anthropic's event stream, frame by frame, as OpenAI chunks.

    Stateful because Anthropic's stream is: the message id, the model and the input token
    count arrive once in ``message_start`` and every later chunk has to repeat them, and
    the stop reason arrives in ``message_delta`` one event before the chunk that carries
    it. Nothing is *accumulated* — each event produces its own frames and is forgotten —
    so a token reaches the client the moment it arrives, which is the whole reason a
    caller asked for a stream.
    """

    def __init__(self, *, include_usage: bool = False) -> None:
        self.include_usage = include_usage
        self.id = ""
        self.model = ""
        self.created = int(time.time())
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason: str | None = None

    def feed(self, data: str) -> list[StreamFrame]:
        """The frames one Anthropic event becomes — usually one, often none."""
        try:
            event = StreamEvent.model_validate_json(data)
        except ValidationError:
            # An event that does not parse is not relayed. Unlike the OpenAI dialect,
            # where the provider's bytes are the client's bytes and passing an unparsed
            # frame through is the honest choice, here a frame the gateway cannot read is
            # a frame it cannot translate — and Anthropic's wire format is not something
            # a client SDK could make sense of anyway.
            logger.warning("could not parse an anthropic stream event")
            return []

        if event.type == "message_start":
            return self._start(event)
        if event.type == "content_block_start":
            block = event.content_block
            return self._text(block.text) if block is not None and block.text else []
        if event.type == "content_block_delta":
            delta = event.delta
            if delta is None or delta.type != "text_delta" or not delta.text:
                # `thinking_delta` and `input_json_delta` are not the assistant speaking.
                return []
            return self._text(delta.text)
        if event.type == "message_delta":
            return self._message_delta(event)
        if event.type == "message_stop":
            return self._stop()
        if event.type == "error":
            raise UpstreamStreamFailed(_error_text(event))
        # `ping`, `content_block_stop`, and whatever Anthropic adds next.
        return []

    # -- per event ----------------------------------------------------------

    def _start(self, event: StreamEvent) -> list[StreamFrame]:
        message = event.message
        if message is not None:
            self.id = completion_id(message.id)
            self.model = message.model
            self.input_tokens = message.usage.input_tokens
            self.output_tokens = message.usage.output_tokens
        # OpenAI's first chunk announces the role and carries no content.
        return [self._frame({"role": "assistant", "content": ""})]

    def _text(self, text: str) -> list[StreamFrame]:
        return [self._frame({"content": text})]

    def _message_delta(self, event: StreamEvent) -> list[StreamFrame]:
        if event.delta is not None and event.delta.stop_reason is not None:
            self.stop_reason = event.delta.stop_reason
        if event.usage is not None:
            # Cumulative, so assigned rather than added.
            self.output_tokens = event.usage.output_tokens
            if event.usage.input_tokens:
                self.input_tokens = event.usage.input_tokens
        return []

    def _stop(self) -> list[StreamFrame]:
        frames = [self._frame({}, finish=finish_reason(self.stop_reason) or "stop")]
        if self.include_usage:
            frames.append(self._usage_frame())
        return frames

    # -- frame construction -------------------------------------------------

    def _frame(self, delta: dict[str, Any], *, finish: str | None = None) -> StreamFrame:
        return _frame(
            {
                "id": self.id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )

    def _usage_frame(self) -> StreamFrame:
        # OpenAI sends usage in its own final chunk with an empty `choices` list, and a
        # client that asked for `include_usage` reads it from exactly there.
        return _frame(
            {
                "id": self.id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [],
                "usage": {
                    "prompt_tokens": self.input_tokens,
                    "completion_tokens": self.output_tokens,
                    "total_tokens": self.input_tokens + self.output_tokens,
                },
            }
        )


def _frame(payload: dict[str, Any]) -> StreamFrame:
    """One downstream frame, with its parsed view built from the same object.

    Serialising and validating the *same* dict is what guarantees ``data`` and ``chunk``
    cannot disagree — which for a translating dialect they otherwise could, and the
    request log's token counts are read off ``chunk`` while the client reads ``data``.
    """
    return StreamFrame(data=json.dumps(payload), chunk=ChatChunk.model_validate(payload))


def _error_text(event: StreamEvent) -> str:
    if event.error is None:
        return "the provider reported an error"
    return f"{event.error.type}: {event.error.message}".strip(": ")


def wants_usage(request: ChatRequest) -> bool:
    """Whether the client asked for usage on the stream, OpenAI's way."""
    options = request.stream_options or {}
    return bool(options.get("include_usage"))


adapter = register(AnthropicAdapter())
