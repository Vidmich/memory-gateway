"""The anthropic dialect: request translation, repairs, responses, and error mapping.

Streaming lives in ``tests/test_adapter_anthropic_stream.py`` and the cross-dialect
agreement in ``tests/test_adapter_contract.py``. This file is the translation itself,
tested by calling the pure functions — which is the reason they are pure.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.anthropic import (
    API_VERSION,
    DEFAULT_MAX_TOKENS,
    DROPPED_PARAMS,
    LEADING_TURN,
    STOP_REASONS,
    AnthropicAdapter,
    max_tokens_for,
    messages_url,
    split_system,
    to_turns,
    translate_request,
)
from app.adapters.base import (
    DialectRejected,
    MalformedUpstreamResponse,
    UpstreamTarget,
    get_adapter,
)
from app.core.ids import uuid7
from app.schemas.openai import ChatMessage, ChatRequest

adapter = AnthropicAdapter()


def target(**overrides: Any) -> UpstreamTarget:
    values: dict[str, Any] = {
        "id": uuid7(),
        "name": "claude",
        "base_url": "https://api.anthropic.com/v1",
        "dialect": "anthropic",
        "upstream_model_id": "claude-sonnet-4-5",
        "auth_type": "api_key_header",
        "credential": "sk-ant-secret",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return UpstreamTarget(**values)


def request(**overrides: Any) -> ChatRequest:
    values: dict[str, Any] = {
        "model": "demo",
        "messages": [{"role": "user", "content": "hi"}],
    }
    values.update(overrides)
    return ChatRequest.model_validate(values)


def turns(*pairs: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=role, content=content) for role, content in pairs]


def body_of(prepared: httpx.Request) -> dict[str, Any]:
    return json.loads(prepared.content)  # type: ignore[no-any-return]


def sent(**overrides: Any) -> dict[str, Any]:
    return translate_request(request(**overrides), target())


# ---------------------------------------------------------------------------
# the endpoint, auth, and version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1/", "https://api.anthropic.com/v1/messages"),
        ("https://gateway.internal", "https://gateway.internal/messages"),
        (
            "https://proxy.test/anthropic?tenant=acme",
            "https://proxy.test/anthropic/messages?tenant=acme",
        ),
    ],
)
def test_the_endpoint_is_messages_not_chat_completions(base_url: str, expected: str) -> None:
    assert messages_url(base_url) == expected


def test_the_api_key_goes_in_x_api_key_with_a_version() -> None:
    prepared = adapter.prepare(request(), target())

    assert prepared.headers["x-api-key"] == "sk-ant-secret"
    assert prepared.headers["anthropic-version"] == API_VERSION
    assert "authorization" not in prepared.headers


def test_the_configured_auth_style_is_honoured_rather_than_forced() -> None:
    """An operator pointing this dialect at a proxy that wants a bearer token gets one.

    The dialect decides the body; the model decides how it authenticates. Forcing
    ``x-api-key`` here would make the auth-type field a lie for anthropic models.
    """
    prepared = adapter.prepare(request(), target(auth_type="bearer"))

    assert prepared.headers["authorization"] == "Bearer sk-ant-secret"
    assert "x-api-key" not in prepared.headers


def test_a_pinned_api_version_wins() -> None:
    """The escape hatch: a provider's breaking change must not need a release here."""
    prepared = adapter.prepare(request(), target(extra_headers={"anthropic-version": "2099-01-01"}))

    assert prepared.headers["anthropic-version"] == "2099-01-01"


def test_the_per_model_timeout_rides_on_the_request() -> None:
    assert adapter.prepare(request(), target(timeout_seconds=7)).extensions["timeout"]["read"] == 7


def test_the_virtual_model_is_replaced_by_the_upstream_id() -> None:
    assert body_of(adapter.prepare(request(model="demo"), target()))["model"] == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# system messages
# ---------------------------------------------------------------------------


def test_a_single_system_message_becomes_the_system_parameter() -> None:
    payload = sent(
        messages=[{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}]
    )

    assert payload["system"] == "Be terse."
    assert [turn["role"] for turn in payload["messages"]] == ["user"]


def test_every_system_message_reaches_the_parameter_in_order() -> None:
    """The assembler stacks the model's context, the gateway's, and the client's own —
    all three arrive as separate system messages, and all three have to survive."""
    system, rest = split_system(
        turns(
            ("system", "You are terse."),
            ("system", "Answer in English."),
            ("user", "hi"),
            ("assistant", "hello"),
            ("system", "Now be formal."),
            ("user", "again"),
        )
    )

    assert system == "You are terse.\n\nAnswer in English.\n\nNow be formal."
    assert [message.role for message in rest] == ["user", "assistant", "user"]


def test_no_system_message_sends_no_system_parameter() -> None:
    assert "system" not in sent()


def test_a_developer_message_is_a_system_message() -> None:
    """OpenAI's newer name for the same role. A dialect that did not know it would demote
    the instructions to a user turn, which reads to the model as the caller talking."""
    payload = sent(
        messages=[{"role": "developer", "content": "Be terse."}, {"role": "user", "content": "hi"}]
    )

    assert payload["system"] == "Be terse."


def test_an_empty_system_message_is_not_sent() -> None:
    assert "system" not in sent(
        messages=[{"role": "system", "content": "   "}, {"role": "user", "content": "hi"}]
    )


# ---------------------------------------------------------------------------
# message-sequence repair
# ---------------------------------------------------------------------------


def test_consecutive_same_role_messages_are_merged() -> None:
    result = to_turns(turns(("user", "first"), ("user", "second"), ("assistant", "ok")))

    assert [turn["role"] for turn in result] == ["user", "assistant"]
    assert result[0]["content"][0]["text"] == "first\n\nsecond"


def test_a_leading_assistant_turn_gets_a_user_turn_in_front() -> None:
    """Replaying a stored conversation that begins with the assistant is ordinary; a 400
    for it would make the compatibility claim false for the most common case there is."""
    result = to_turns(turns(("assistant", "Welcome!"), ("user", "hi")))

    assert result[0] == {"role": "user", "content": [{"type": "text", "text": LEADING_TURN}]}
    assert result[1]["content"][0]["text"] == "Welcome!"


def test_a_trailing_assistant_turn_is_right_stripped() -> None:
    """Anthropic reads a trailing assistant turn as a prefill to continue from, and
    refuses to continue from whitespace."""
    result = to_turns(turns(("user", "hi"), ("assistant", "The answer is  ")))

    assert result[-1]["content"][0]["text"] == "The answer is"


def test_a_trailing_user_turn_keeps_its_whitespace() -> None:
    """The rule is about prefills, not about tidiness — the caller's own text is theirs."""
    result = to_turns(turns(("user", "hi  ")))

    assert result[-1]["content"][0]["text"] == "hi  "


def test_empty_messages_are_dropped() -> None:
    result = to_turns(turns(("user", "hi"), ("assistant", ""), ("user", "still there?")))

    assert [turn["role"] for turn in result] == ["user"]
    assert result[0]["content"][0]["text"] == "hi\n\nstill there?"


def test_a_conversation_of_nothing_but_blanks_still_asks_something() -> None:
    """An empty ``messages`` array is a 400 from the provider. One minimal turn is a
    question that can be answered, and the caller sees what they sent in the transcript."""
    assert to_turns(turns(("user", "   "), ("assistant", ""))) == [
        {"role": "user", "content": [{"type": "text", "text": LEADING_TURN}]}
    ]


def test_the_repairs_compose() -> None:
    """The case that breaks a translator written one rule at a time: merging two assistant
    turns produces a leading assistant turn, which then needs the user turn in front."""
    result = to_turns(turns(("assistant", "a"), ("assistant", "b "), ("user", "hi")))

    assert [turn["role"] for turn in result] == ["user", "assistant", "user"]
    assert result[1]["content"][0]["text"] == "a\n\nb "


def test_multi_part_content_is_read_for_its_text() -> None:
    """Vision is out of scope (SPEC §16), but the words next to the image are not."""
    result = to_turns(
        [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                ],
            )
        ]
    )

    assert result[0]["content"] == [{"type": "text", "text": "What is this?"}]


def test_a_tool_result_becomes_a_user_turn_rather_than_vanishing() -> None:
    result = to_turns(turns(("user", "hi"), ("assistant", "one moment"), ("tool", "42")))

    assert [turn["role"] for turn in result] == ["user", "assistant", "user"]
    assert result[-1]["content"][0]["text"] == "42"


def test_content_is_always_a_block_list() -> None:
    assert sent()["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_max_tokens_is_always_present() -> None:
    """Anthropic requires it and OpenAI does not, so a client that never sends one — which
    is most of them — would otherwise get a 400 on every request."""
    assert sent()["max_tokens"] == DEFAULT_MAX_TOKENS


def test_the_clients_max_tokens_wins() -> None:
    assert sent(max_tokens=64)["max_tokens"] == 64


def test_max_completion_tokens_is_read_too() -> None:
    """OpenAI's newer name for the field. A client using it must not silently fall back to
    the default."""
    assert sent(max_completion_tokens=128)["max_tokens"] == 128


def test_the_models_default_params_supply_one_when_the_merge_is_skipped() -> None:
    """The connectivity probe builds its own request and never sees ``resolve_params``."""
    assert max_tokens_for(request(), target(default_params={"max_tokens": 256})) == 256


def test_a_nonsense_max_tokens_falls_through_to_the_default() -> None:
    assert (
        max_tokens_for(request(), target(default_params={"max_tokens": True})) == DEFAULT_MAX_TOKENS
    )


def test_temperature_is_clamped_to_the_anthropic_range() -> None:
    """OpenAI runs to 2. A 400 the caller cannot act on is worse than the closest thing
    this provider offers."""
    assert sent(temperature=1.7)["temperature"] == 1.0
    assert sent(temperature=0.3)["temperature"] == 0.3


def test_top_p_passes_through() -> None:
    assert sent(top_p=0.9)["top_p"] == 0.9


def test_stop_becomes_stop_sequences() -> None:
    assert sent(stop="END")["stop_sequences"] == ["END"]
    assert sent(stop=["A", "B"])["stop_sequences"] == ["A", "B"]


def test_an_empty_stop_list_is_not_sent() -> None:
    assert "stop_sequences" not in sent(stop=[])


@pytest.mark.parametrize("name", DROPPED_PARAMS)
def test_a_dropped_parameter_never_reaches_the_wire(name: str) -> None:
    """Anthropic rejects a body field it does not recognise, so forwarding an OpenAI
    parameter would turn every one of these into a 400 from the provider."""
    values = {"presence_penalty": 0.5, "frequency_penalty": 0.5, "n": 1, "seed": 7}
    payload = sent(
        **{name: values.get(name, {"type": "json_object"} if name == "response_format" else 1)}
    )

    assert name not in payload


def test_the_dropped_set_is_reported_for_the_log() -> None:
    assert adapter.dropped({"temperature", "presence_penalty", "seed"}) == (
        "presence_penalty",
        "seed",
    )


def test_nothing_is_reported_when_nothing_was_dropped() -> None:
    assert adapter.dropped({"temperature", "top_p", "max_tokens", "stop"}) == ()


def test_asking_for_several_completions_is_refused_rather_than_answered_once() -> None:
    """The one parameter that cannot be dropped quietly: a caller who asked for three and
    silently received one has no way to notice until it matters."""
    with pytest.raises(DialectRejected) as failure:
        translate_request(request(n=3), target())

    assert failure.value.param == "n"
    assert "'n' must be 1" in failure.value.message


def test_asking_for_exactly_one_completion_is_fine() -> None:
    assert "n" not in sent(n=1)


def test_unknown_client_fields_are_not_forwarded() -> None:
    """The opposite of the openai dialect, and deliberately so — see the module docstring.
    Anthropic 400s on a body field it does not know."""
    assert "reasoning_effort" not in sent(reasoning_effort="high")


def test_stream_options_stay_between_the_client_and_the_gateway() -> None:
    assert "stream_options" not in sent(stream=True, stream_options={"include_usage": True})


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


def message(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 25},
    }
    payload.update(overrides)
    return payload


def parsed(**overrides: Any) -> Any:
    return adapter.parse(httpx.Response(200, json=message(**overrides)))


def test_a_message_becomes_a_chat_completion() -> None:
    result = parsed()

    assert result.object == "chat.completion"
    assert result.model == "claude-sonnet-4-5-20250929"
    assert result.choices[0].message is not None
    assert result.choices[0].message.content == "hello"
    assert result.choices[0].message.role == "assistant"


def test_text_blocks_are_concatenated_in_order() -> None:
    result = parsed(
        content=[
            {"type": "text", "text": "one "},
            {"type": "text", "text": "two"},
        ]
    )

    assert result.choices[0].message is not None
    assert result.choices[0].message.content == "one two"


def test_a_thinking_block_is_not_part_of_the_completion() -> None:
    """Extended thinking is out of scope, and rendering it as the assistant's words would
    be worse than leaving it out: the caller would read a monologue as an answer."""
    result = parsed(
        content=[
            {"type": "thinking", "thinking": "the user wants..."},
            {"type": "text", "text": "42"},
        ]
    )

    assert result.choices[0].message is not None
    assert result.choices[0].message.content == "42"


def test_the_response_id_is_openai_shaped_and_still_names_the_message() -> None:
    """A support conversation that starts with a response id should end at the provider's
    own logs without a lookup table."""
    assert parsed().id == "chatcmpl-msg_01ABC"


def test_a_response_with_no_id_still_gets_one() -> None:
    assert parsed(id="").id.startswith("chatcmpl-")


@pytest.mark.parametrize(("stop_reason", "expected"), sorted(STOP_REASONS.items()))
def test_stop_reason_mapping(stop_reason: str, expected: str) -> None:
    assert parsed(stop_reason=stop_reason).choices[0].finish_reason == expected


def test_an_unmapped_stop_reason_still_says_the_generation_ended() -> None:
    """A null ``finish_reason`` reads as "still going" to more than one client library."""
    assert parsed(stop_reason="something_new").choices[0].finish_reason == "stop"


def test_usage_is_translated_and_totalled() -> None:
    """Anthropic sends no total, and a client reading one for a cost estimate must not get
    a zero — task 14's limits and task 07's charts read the same three numbers."""
    usage = parsed().usage

    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (10, 25, 35)


def test_a_body_that_is_not_a_message_is_refused() -> None:
    with pytest.raises(MalformedUpstreamResponse):
        adapter.parse(httpx.Response(200, text="<html>gateway timeout</html>"))


def test_an_unknown_content_block_type_does_not_break_the_translation() -> None:
    """A provider shipping a new block type must not turn every response into a 502."""
    result = parsed(content=[{"type": "video", "url": "…"}, {"type": "text", "text": "ok"}])

    assert result.choices[0].message is not None
    assert result.choices[0].message.content == "ok"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def failure(status: int, error_type: str, message: str = "no") -> Any:
    return adapter.error(
        httpx.Response(
            status, json={"type": "error", "error": {"type": error_type, "message": message}}
        )
    )


@pytest.mark.parametrize(
    ("status", "anthropic_type", "expected_type"),
    [
        (400, "invalid_request_error", "invalid_request_error"),
        (401, "authentication_error", "authentication_error"),
        (403, "permission_error", "invalid_request_error"),
        (404, "not_found_error", "invalid_request_error"),
        (413, "request_too_large", "invalid_request_error"),
        (429, "rate_limit_error", "rate_limit_error"),
        (500, "api_error", "api_error"),
    ],
)
def test_error_types_map_to_openais_vocabulary(
    status: int, anthropic_type: str, expected_type: str
) -> None:
    result = failure(status, anthropic_type)

    assert result.status_code == status
    assert result.type == expected_type
    # The provider's own type survives as the code, which is the specific half.
    assert result.code == anthropic_type


def test_the_upstream_message_is_preserved() -> None:
    assert failure(
        429, "rate_limit_error", "Number of requests has exceeded your rate limit"
    ).message == ("Number of requests has exceeded your rate limit")


def test_overloaded_becomes_a_retryable_503() -> None:
    """SPEC §8.2's retry table has never heard of a 529 and treats an unrecognised status
    as final — which is the opposite of what a provider asking to be retried wants."""
    result = failure(529, "overloaded_error", "Overloaded")

    assert result.status_code == 503
    assert result.code == "overloaded_error"


def test_overloaded_under_some_other_status_is_still_a_503() -> None:
    assert failure(500, "overloaded_error").status_code == 503


def test_a_body_that_is_not_an_error_envelope_still_produces_a_message() -> None:
    result = adapter.error(httpx.Response(502, text="<html>bad gateway</html>"))

    assert result.status_code == 502
    assert "bad gateway" in result.message


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_the_dialect_is_registered() -> None:
    assert get_adapter("anthropic").dialect == "anthropic"


def test_the_credential_is_not_in_the_targets_repr() -> None:
    assert "sk-ant-secret" not in repr(target())
