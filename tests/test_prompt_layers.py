"""SPEC §7 end to end: the five layers, the token budget, and the overflow guard.

``tests/test_prompt_assembly.py`` covers task 02's simpler seam. This file is about the
full function — the one the request path calls — and it is deliberately heavy on golden
files, because the thing most likely to break here is the *wording*, and wording does not
fail a length assertion.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.openai import ChatMessage
from app.services.prompt import (
    COMPLETION_RESERVE_TOKENS,
    DROPPED_BUDGET,
    DROPPED_CONTEXT,
    REFERENCE_HEADING,
    Assembled,
    Layer,
    assemble,
    fit_documents,
    render_documents,
)
from app.services.tokenizer import WordTokenizer, count
from tests.prompt_support import assert_golden, chunk

TOKENIZER = WordTokenizer()

#: Big enough that nothing is truncated unless a test means it to be.
ROOMY = 100_000


def ask(*pairs: tuple[str, str]) -> list[ChatMessage]:
    return [ChatMessage(role=role, content=content) for role, content in pairs]


def system_of(result: Assembled) -> str:
    message = result.messages[0]
    assert message.role == "system"
    return str(message.content)


def layer(result: Assembled, name: str) -> Layer:
    return next(item for item in result.layers if item.name == name)


# ---------------------------------------------------------------------------
# the layer matrix
# ---------------------------------------------------------------------------


def test_every_layer_present_renders_the_shape_the_spec_prints() -> None:
    result = assemble(
        ask(("system", "Answer in British English."), ("user", "How do refunds work?")),
        model_context="You are terse.",
        gateway_context="You are Acme's support assistant.",
        chunks=[
            chunk("Refunds are issued within 14 days.", score=0.81, section="p. 12"),
            chunk(
                "Shipping is free above 50 euros.",
                score=0.62,
                source="pricing.md",
                document="22222222-2222-5222-8222-222222222222",
            ),
        ],
        facts=["Prefers concise answers with code examples.", "Works in Python."],
        doc_max_tokens=ROOMY,
        memory_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )

    assert_golden("all_layers", system_of(result))


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("only_documents", {"chunks": [chunk("Refunds take 14 days.")]}),
        ("only_gateway_context", {"gateway_context": "Be brief."}),
        ("only_client_system", {}),
        (
            "documents_without_contexts",
            {
                "chunks": [chunk("Refunds take 14 days.", section="§4")],
                "facts": ["Prefers Python."],
            },
        ),
    ],
)
def test_empty_layers_are_omitted_with_their_delimiter(
    name: str, kwargs: dict[str, object]
) -> None:
    """No orphan headings, and no double blank lines where a layer used to be."""
    result = assemble(
        ask(("system", "Be polite."), ("user", "hi")),
        doc_max_tokens=ROOMY,
        memory_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
        **kwargs,  # type: ignore[arg-type]
    )

    assert_golden(name, system_of(result))


def test_no_layers_at_all_leaves_the_request_untouched() -> None:
    original = ask(("user", "hi"))

    result = assemble(original, doc_max_tokens=ROOMY, tokenizer=TOKENIZER)

    assert list(result.messages) == original


def test_an_empty_chunk_list_renders_no_reference_block() -> None:
    """Not an empty heading. A model told there is reference material and shown none is
    being invited to cite something that does not exist."""
    result = assemble(
        ask(("user", "hi")),
        gateway_context="Be brief.",
        chunks=[],
        doc_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )

    assert REFERENCE_HEADING not in system_of(result)
    assert result.memory_tokens == 0


def test_multiple_client_system_messages_are_concatenated_in_order() -> None:
    result = assemble(
        ask(("system", "one"), ("user", "hi"), ("system", "two")),
        gateway_context="ctx",
        doc_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )

    assert system_of(result) == "ctx\n\none\n\ntwo"


def test_conversation_turns_are_forwarded_unchanged() -> None:
    result = assemble(
        ask(("user", "a"), ("assistant", "b"), ("user", "c")),
        gateway_context="ctx",
        chunks=[chunk("x")],
        doc_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )

    assert [(m.role, m.content) for m in result.messages[1:]] == [
        ("user", "a"),
        ("assistant", "b"),
        ("user", "c"),
    ]


def test_multipart_system_content_contributes_its_text() -> None:
    """The array form of `content` (the one used for images). Its text parts merge into
    the layering; an image in a system message is not something layering can merge."""
    original = [
        ChatMessage(
            role="system",
            content=[{"type": "text", "text": "from parts"}, {"type": "image_url", "url": "x"}],
        ),
        ChatMessage(role="user", content="hi"),
    ]

    result = assemble(original, model_context="ctx", tokenizer=TOKENIZER)

    assert system_of(result) == "ctx\n\nfrom parts"


def test_multipart_user_content_is_forwarded_whole() -> None:
    """Flattening it would strip the image out of a vision request."""
    parts: list[dict[str, Any]] = [{"type": "text", "text": "look"}]
    original = [ChatMessage(role="user", content=parts)]

    result = assemble(original, model_context="ctx", tokenizer=TOKENIZER)

    assert result.messages[1].content == parts


def test_the_document_block_comes_before_the_memory_block() -> None:
    """SPEC §7's order. Both are "things the gateway knows", and the one about the
    organization has to be read before the one about the person."""
    result = assemble(
        ask(("user", "hi")),
        chunks=[chunk("Refunds take 14 days.")],
        facts=["Prefers Python."],
        doc_max_tokens=ROOMY,
        memory_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )
    text = system_of(result)

    assert text.index("## Reference material") < text.index("## What you know about this user")


def test_entries_are_numbered_from_one_so_a_citation_resolves() -> None:
    text = render_documents([chunk("first"), chunk("second", index=1)])

    assert "[1] source: handbook.md" in text
    assert "[2] source: handbook.md" in text


def test_a_section_is_rendered_in_brackets_and_omitted_when_absent() -> None:
    assert "source: handbook.md (p. 12)" in render_documents([chunk("x", section="p. 12")])
    assert "source: handbook.md\n" in render_documents([chunk("x")])


# ---------------------------------------------------------------------------
# token budgeting
# ---------------------------------------------------------------------------


def test_the_budget_covers_the_whole_block_including_its_heading() -> None:
    """`doc_max_tokens` is a cap somebody can verify by counting what arrived upstream,
    so the boilerplate is inside it rather than free."""
    chunks = [chunk("word " * 50, index=i) for i in range(4)]

    result = fit_documents(chunks, budget=200, tokenizer=TOKENIZER)

    assert count(TOKENIZER, result.text) <= 200
    assert result.kept and result.dropped


def test_a_budget_too_small_for_the_boilerplate_injects_nothing() -> None:
    """Rather than a heading with no excerpts under it."""
    result = fit_documents([chunk("hello")], budget=5, tokenizer=TOKENIZER)

    assert result.kept == ()
    assert result.text == ""


def test_chunks_are_dropped_from_the_tail_which_is_the_lowest_score() -> None:
    chunks = [
        chunk("aaa " * 40, score=0.9, index=0),
        chunk("bbb " * 40, score=0.7, index=1),
        chunk("ccc " * 40, score=0.5, index=2),
    ]

    result = fit_documents(chunks, budget=120, tokenizer=TOKENIZER)

    assert [item.score for item in result.kept] == [0.9]
    assert [item.score for item in result.dropped] == [0.7, 0.5]


def test_exactly_at_the_boundary_the_chunk_is_kept() -> None:
    """An off-by-one here silently costs a chunk on every request that fits perfectly."""
    one = [chunk("alpha beta gamma")]
    exact = count(TOKENIZER, render_documents(one))

    assert fit_documents(one, budget=exact, tokenizer=TOKENIZER).kept == tuple(one)
    assert fit_documents(one, budget=exact - 1, tokenizer=TOKENIZER).kept == ()


def test_the_budget_is_never_exceeded_whatever_the_input() -> None:
    chunks = [chunk("lorem ipsum dolor sit amet " * 20, index=i) for i in range(10)]

    for budget in (0, 1, 60, 100, 250, 1000):
        result = fit_documents(chunks, budget=budget, tokenizer=TOKENIZER)
        assert count(TOKENIZER, result.text) <= budget


def test_a_dropped_chunk_records_which_limit_dropped_it() -> None:
    chunks = [chunk("word " * 60, index=i) for i in range(3)]

    result = assemble(ask(("user", "hi")), chunks=chunks, doc_max_tokens=80, tokenizer=TOKENIZER)

    assert [reason for _, reason in result.dropped] == [DROPPED_BUDGET] * len(result.dropped)
    assert result.dropped


def test_memory_tokens_count_both_blocks() -> None:
    result = assemble(
        ask(("user", "hi")),
        chunks=[chunk("Refunds take 14 days.")],
        facts=["Prefers Python."],
        doc_max_tokens=ROOMY,
        memory_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )

    assert (
        result.memory_tokens == layer(result, "documents").tokens + layer(result, "memory").tokens
    )
    assert result.memory_tokens > 0


def test_the_memory_block_is_dropped_whole_when_it_does_not_fit() -> None:
    """A truncated list of facts about somebody is worse than none: half a fact reads as
    a whole one."""
    result = assemble(
        ask(("user", "hi")),
        facts=["Works primarily in Python and Terraform, and prefers concise answers."],
        memory_max_tokens=3,
        tokenizer=TOKENIZER,
    )

    assert layer(result, "memory").text == ""


def test_the_document_block_is_truncated_before_the_memory_block() -> None:
    """SPEC §7 says so explicitly, and the order matters: documents are the bulk, and
    truncating memory first would spend the whole window on excerpts."""
    result = assemble(
        ask(("user", "hi")),
        chunks=[chunk("word " * 200, index=i) for i in range(3)],
        facts=["Prefers Python."],
        doc_max_tokens=100,
        memory_max_tokens=ROOMY,
        context_window=COMPLETION_RESERVE_TOKENS + 400,
        tokenizer=TOKENIZER,
    )

    assert layer(result, "memory").text != ""
    assert len(result.dropped) > 0


# ---------------------------------------------------------------------------
# the context-window guard
# ---------------------------------------------------------------------------


def test_a_conversation_that_fills_the_window_injects_nothing_and_says_so() -> None:
    long_turn = "word " * 500

    result = assemble(
        ask(("user", long_turn)),
        chunks=[chunk("Refunds take 14 days.")],
        doc_max_tokens=ROOMY,
        context_window=400,
        tokenizer=TOKENIZER,
    )

    assert result.overflowed is True
    assert result.injected == ()
    assert all(REFERENCE_HEADING not in str(m.content) for m in result.messages)
    # The request still goes upstream. Refusing it would be a worse answer than an
    # ungrounded one, and the flag is how somebody finds out.
    assert result.messages[-1].content == long_turn


def test_an_overflowing_request_blames_the_context_window_not_the_budget() -> None:
    result = assemble(
        ask(("user", "word " * 500)),
        chunks=[chunk("Refunds take 14 days.")],
        doc_max_tokens=ROOMY,
        context_window=400,
        tokenizer=TOKENIZER,
    )

    assert [reason for _, reason in result.dropped] == [DROPPED_CONTEXT]


def test_an_unknown_context_window_does_not_fire_the_guard() -> None:
    """`None` means nobody has told us the window, not that it is zero. Guessing would
    withhold memory from requests a provider would have served perfectly well."""
    result = assemble(
        ask(("user", "word " * 5000)),
        chunks=[chunk("Refunds take 14 days.")],
        doc_max_tokens=ROOMY,
        context_window=None,
        tokenizer=TOKENIZER,
    )

    assert result.overflowed is False
    assert len(result.injected) == 1


def test_a_window_with_a_little_room_shrinks_the_budget_rather_than_dropping_everything() -> None:
    """All-or-nothing would throw away the top-scoring excerpt to save the second."""
    chunks = [chunk("alpha " * 30, index=0), chunk("beta " * 30, index=1)]

    result = assemble(
        ask(("user", "word " * 100)),
        chunks=chunks,
        doc_max_tokens=ROOMY,
        # Room for exactly one of the two blocks, boilerplate included.
        context_window=100 + COMPLETION_RESERVE_TOKENS + 90,
        tokenizer=TOKENIZER,
    )

    assert result.overflowed is False
    assert len(result.injected) == 1
    assert len(result.dropped) == 1


def test_the_guard_reserves_room_for_the_answer() -> None:
    """A prompt that fills the window exactly is one the provider accepts and then has
    nowhere to write into."""
    turn = "word " * 100
    used = count(TOKENIZER, turn)

    result = assemble(
        ask(("user", turn)),
        chunks=[chunk("Refunds take 14 days.")],
        doc_max_tokens=ROOMY,
        context_window=used + 10,
        tokenizer=TOKENIZER,
    )

    assert result.overflowed is True


# ---------------------------------------------------------------------------
# the log record
# ---------------------------------------------------------------------------


def test_the_chunk_log_records_every_chunk_and_what_became_of_it() -> None:
    chunks = [chunk("word " * 60, index=i) for i in range(3)]

    result = assemble(ask(("user", "hi")), chunks=chunks, doc_max_tokens=80, tokenizer=TOKENIZER)
    entries = result.chunk_log()

    assert len(entries) == 3
    assert sum(1 for entry in entries if entry["injected"]) == len(result.injected)
    assert all("dropped" in entry for entry in entries if not entry["injected"])


def test_the_chunk_log_keeps_the_name_rather_than_only_the_id() -> None:
    """The vector store may be reindexed before anyone opens the drawer; what the request
    retrieved is a fact about the request."""
    result = assemble(
        ask(("user", "hi")),
        chunks=[chunk("x", source="handbook.md", section="p. 12")],
        doc_max_tokens=ROOMY,
        tokenizer=TOKENIZER,
    )

    entry = result.chunk_log()[0]
    assert entry["source_name"] == "handbook.md"
    assert entry["page_or_section"] == "p. 12"
    assert entry["score"] == pytest.approx(0.8)
