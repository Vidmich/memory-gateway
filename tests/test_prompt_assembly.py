"""Prompt layering (SPEC §7) and parameter resolution."""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.openai import ChatMessage
from app.services.params import resolve_params
from app.services.prompt import PromptAssembler, PromptLayer

assembler = PromptAssembler()


def messages(*pairs: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=role, content=content) for role, content in pairs]


def test_layers_are_prepended_as_one_system_message() -> None:
    result = assembler.assemble(
        messages(("user", "hi")),
        [PromptLayer("model", "You are terse."), PromptLayer("gateway", "Answer in English.")],
    )

    assert [message.role for message in result] == ["system", "user"]
    assert result[0].content == "You are terse.\n\nAnswer in English."


def test_layer_order_is_model_then_gateway() -> None:
    result = assembler.assemble(
        messages(("user", "hi")),
        [PromptLayer("model", "FIRST"), PromptLayer("gateway", "SECOND")],
    )

    assert result[0].content == "FIRST\n\nSECOND"


def test_client_system_message_comes_last_and_survives() -> None:
    result = assembler.assemble(
        messages(("system", "Be brief."), ("user", "hi")),
        [PromptLayer("model", "You are terse.")],
    )

    assert result[0].content == "You are terse.\n\nBe brief."
    assert [message.role for message in result] == ["system", "user"]


def test_multiple_client_system_messages_are_concatenated_in_order() -> None:
    result = assembler.assemble(
        messages(("system", "one"), ("user", "hi"), ("system", "two")),
        [],
    )

    assert result[0].content == "one\n\ntwo"


def test_conversation_turns_are_forwarded_unchanged() -> None:
    original = messages(("user", "a"), ("assistant", "b"), ("user", "c"))

    result = assembler.assemble(original, [PromptLayer("model", "ctx")])

    assert [(m.role, m.content) for m in result[1:]] == [
        ("user", "a"),
        ("assistant", "b"),
        ("user", "c"),
    ]


@pytest.mark.parametrize("value", [None, "", "   ", "\n\n"])
def test_empty_layers_are_dropped_with_their_delimiter(value: str | None) -> None:
    result = assembler.assemble(
        messages(("user", "hi")),
        [PromptLayer("model", value), PromptLayer("gateway", "kept")],
    )

    assert result[0].content == "kept"


def test_nothing_to_prepend_leaves_the_request_untouched() -> None:
    original = messages(("user", "hi"))

    result = assembler.assemble(original, [PromptLayer("model", None)])

    assert result == original


def test_multipart_system_content_contributes_its_text() -> None:
    original = [
        ChatMessage(
            role="system",
            content=[{"type": "text", "text": "from parts"}, {"type": "image_url", "url": "x"}],
        ),
        ChatMessage(role="user", content="hi"),
    ]

    result = assembler.assemble(original, [PromptLayer("model", "ctx")])

    assert result[0].content == "ctx\n\nfrom parts"


def test_multipart_user_content_is_not_flattened() -> None:
    parts: list[dict[str, Any]] = [{"type": "text", "text": "look"}]
    original = [ChatMessage(role="user", content=parts)]

    result = assembler.assemble(original, [PromptLayer("model", "ctx")])

    assert result[1].content == parts


# -- parameters --------------------------------------------------------------


def test_gateway_overrides_beat_model_defaults() -> None:
    resolved = resolve_params(
        model_defaults={"temperature": 0.2, "top_p": 0.9},
        gateway_overrides={"temperature": 0.7},
        client={},
    )

    assert resolved.values == {"temperature": 0.7, "top_p": 0.9}


def test_client_wins_over_both() -> None:
    """v1: an override is a default, not a cap. Task 06 adds locked params for pinning."""
    resolved = resolve_params(
        model_defaults={"temperature": 0.2},
        gateway_overrides={"temperature": 0.7},
        client={"temperature": 1.0},
    )

    assert resolved.values["temperature"] == 1.0


def test_missing_layers_are_treated_as_empty() -> None:
    assert resolve_params(model_defaults=None, gateway_overrides=None, client=None).values == {}
