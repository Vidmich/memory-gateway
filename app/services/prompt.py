"""Prompt assembly (SPEC §7).

One system message, built by layered prepend in a fixed order. Task 02 supplies layers 1,
2 and 5; tasks 10 and 12 add the retrieved-document and end-user-memory blocks by passing
more layers to the same call, which is why this is a seam rather than two string
concatenations inside the proxy route.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.schemas.openai import ChatMessage

SYSTEM_ROLE = "system"


@dataclass(frozen=True, slots=True)
class PromptLayer:
    """One contribution to the assembled system message. Empty layers are dropped."""

    name: str
    text: str | None


class PromptAssembler:
    """Fold the layers and the client's own system messages into a single system turn."""

    def assemble(
        self,
        messages: Sequence[ChatMessage],
        layers: Iterable[PromptLayer],
    ) -> list[ChatMessage]:
        conversation = [message for message in messages if message.role != SYSTEM_ROLE]
        client_system = [message for message in messages if message.role == SYSTEM_ROLE]

        parts = [text for layer in layers if (text := _clean(layer.text))]
        # Layer 5: the client's own system messages, verbatim and in order. Multiple ones
        # are concatenated rather than dropped — SPEC §7.
        parts.extend(
            text for message in client_system if (text := _clean(as_text(message.content)))
        )

        if not parts:
            return list(messages)

        assembled = ChatMessage(role=SYSTEM_ROLE, content="\n\n".join(parts))
        return [assembled, *conversation]


def as_text(content: str | list[dict[str, object]] | None) -> str:
    """Flatten message content to plain text.

    Multi-part content (the array form used for images) contributes only its text parts;
    an image in a system message is not something the layering can merge.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _clean(text: str | None) -> str:
    return text.strip() if text else ""
