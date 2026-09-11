"""Citation resolution (task 100), without a provider or a route.

The grammar is the part most likely to be quietly wrong — a coding assistant's answers
are full of ``arr[0]`` — so it gets an adversarial corpus, and the streaming scanner is
driven over every split point of a handle, because "it works when the whole handle is in
one frame" is the case that needs no test.
"""

from __future__ import annotations

import json

import pytest

from app.schemas.openai import ChatChunk, ChatResponse, StreamFrame
from app.services.citations import (
    MODE_FOOTER,
    MODE_METADATA,
    MODE_OFF,
    StreamCitations,
    deliver,
    footer,
    inspector_url,
    resolve,
)
from app.services.retrieval import Chunk


def chunk(index: int, *, source: str = "handbook.pdf", section: str | None = None) -> Chunk:
    return Chunk(
        id=f"doc:{index}",
        score=0.9,
        text=f"excerpt {index}",
        source_name=source,
        page_or_section=section,
        document_id="11111111-1111-5111-8111-111111111111",
        connector_id="22222222-2222-5222-8222-222222222222",
        chunk_index=index,
    )


INJECTED = (chunk(0, section="p. 12"), chunk(1, source="pricing.md"), chunk(2))


def handles(text: str) -> list[int]:
    return [citation.handle for citation in resolve(text, INJECTED).cited]


# ---------------------------------------------------------------------------
# grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("As [2] notes.", [2]),
        ("Both [1, 3] agree.", [1, 3]),
        ("Both [1,3] agree.", [1, 3]),
        ("Adjacent [2][3].", [2, 3]),
        ("A range [1-3].", [1, 2, 3]),
        ("An en-dash range [1" + chr(0x2013) + "2].", [1, 2]),
        ("A footnote[^2] form.", [2]),
        ("Inside a link: [see [2]](http://x).", [2]),
        ("In parentheses ([3]).", [3]),
    ],
)
def test_every_handle_form_models_emit_resolves(text: str, expected: list[int]) -> None:
    assert handles(text) == expected


def test_cited_is_deduplicated_and_ordered_by_first_appearance() -> None:
    assert handles("[3] then [1] then [3] again and [1, 2]") == [3, 1, 2]


def test_a_handle_beyond_the_injected_count_is_unresolved() -> None:
    resolution = resolve("see [2] and [7] and [0]", INJECTED)

    assert [c.handle for c in resolution.cited] == [2]
    assert resolution.unresolved == (7, 0)


def test_a_mixed_handle_splits_into_resolved_and_unresolved() -> None:
    resolution = resolve("see [2, 7]", INJECTED)

    assert [c.handle for c in resolution.cited] == [2]
    assert resolution.unresolved == (7,)
    assert resolution.spans[0].rewrite() == "[2]"


def test_fenced_code_is_skipped_whole() -> None:
    text = "Use it like this:\n```python\nfirst = items[1]\n```\nas [2] says."

    resolution = resolve(text, INJECTED)

    assert [c.handle for c in resolution.cited] == [2]
    assert resolution.unresolved == ()


def test_tilde_fences_count_too() -> None:
    assert handles("~~~\nx[1]\n~~~\n[3]") == [3]


def test_an_unclosed_fence_swallows_the_rest() -> None:
    assert handles("```\n[1] [2]") == []


def test_an_array_index_in_prose_is_not_a_citation() -> None:
    """The one case a fence does not cover: a coding assistant explaining ``items[0]``
    mid-sentence. A word character before the bracket is what tells them apart."""
    resolution = resolve("Read items[0] and matrix[1][2] before [3].", INJECTED)

    assert [c.handle for c in resolution.cited] == [3]
    assert resolution.unresolved == ()


def test_a_document_about_citation_syntax_is_the_adversarial_corpus() -> None:
    """Prose that *talks about* handles. Everything bracketed that is not in code and not
    an index is a handle by the grammar — that is the honest reading, and the point of
    the test is that nothing crashes and the counts are what the grammar says."""
    text = (
        "Citations look like [1] or [1, 2]. Year ranges such as [1990-2020] are not "
        "handles, and neither is a [1000]. `inline[0]` code is not skipped, but `x[0]` "
        "has a word before it. Empty brackets [] and [a] are ignored."
    )

    resolution = resolve(text, INJECTED)

    assert [c.handle for c in resolution.cited] == [1, 2]
    assert resolution.unresolved == ()


def test_a_long_range_is_two_numbers_not_a_hundred() -> None:
    resolution = resolve("[1-100]", INJECTED)

    assert resolution.unresolved == (100,)
    assert [c.handle for c in resolution.cited] == [1]


def test_nothing_injected_resolves_nothing() -> None:
    resolution = resolve("as [1] says", ())

    assert resolution.cited == ()
    assert resolution.unresolved == (1,)


def test_empty_text_is_an_empty_resolution() -> None:
    assert resolve(None, INJECTED) == resolve("", INJECTED)
    assert resolve("", INJECTED).cited == ()


# ---------------------------------------------------------------------------
# stripping and the footer
# ---------------------------------------------------------------------------


def test_strip_removes_unresolved_handles_and_the_space_before_them() -> None:
    text = "It says [7]. Also [2, 9] and [8][2]."

    stripped = resolve(text, INJECTED).strip(text)

    assert stripped == "It says. Also [2] and [2]."


def test_strip_leaves_resolved_handles_byte_for_byte() -> None:
    text = "Keep [2], keep [1,3], keep [1-2]."

    assert resolve(text, INJECTED).strip(text) == text


def test_strip_leaves_code_alone() -> None:
    text = "```\nx = a[9]\n```\n[9]"

    assert resolve(text, INJECTED).strip(text) == "```\nx = a[9]\n```\n"


def test_footer_uses_the_models_handles_in_first_appearance_order() -> None:
    resolution = resolve("[3] first, then [1].", INJECTED)

    assert footer(resolution.cited) == "\n\nSources:\n[3] handbook.pdf\n[1] handbook.pdf (p. 12)"


def test_footer_links_when_a_ui_address_is_known() -> None:
    resolution = resolve("[2]", INJECTED)

    text = footer(resolution.cited, base_url="https://ui.example/")

    assert text == (
        "\n\nSources:\n[2] [pricing.md](https://ui.example/connectors/"
        "22222222-2222-5222-8222-222222222222?document=11111111-1111-5111-8111-111111111111"
        "&chunk=doc%3A1)"
    )


def test_footer_is_empty_when_nothing_was_cited() -> None:
    assert footer(()) == ""


def test_inspector_url_needs_a_connector_and_a_document() -> None:
    orphan = Chunk(
        id="x", score=1.0, text="", source_name="s", page_or_section=None,
        document_id=None, connector_id=None, chunk_index=0,
    )  # fmt: skip

    assert inspector_url("https://ui", orphan) is None
    assert inspector_url(None, INJECTED[0]) is None


def test_citation_json_carries_what_the_prompt_already_said_and_the_ids() -> None:
    [citation] = resolve("[1]", INJECTED).cited

    payload = citation.as_json(base_url="https://ui")

    assert payload == {
        "handle": 1,
        "chunk_id": "doc:0",
        "document_id": "11111111-1111-5111-8111-111111111111",
        "document_name": "handbook.pdf",
        "connector_id": "22222222-2222-5222-8222-222222222222",
        "section": "p. 12",
        "chunk_strategy": None,
        "matched_text": None,
        "url": (
            "https://ui/connectors/22222222-2222-5222-8222-222222222222"
            "?document=11111111-1111-5111-8111-111111111111&chunk=doc%3A0"
        ),
    }


# ---------------------------------------------------------------------------
# non-streaming delivery
# ---------------------------------------------------------------------------


def response(content: str) -> ChatResponse:
    return ChatResponse.model_validate(
        {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}},
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


def test_off_returns_the_same_object_and_still_resolves() -> None:
    original = response("see [2] and [7]")

    delivered = deliver(original, INJECTED, mode=MODE_OFF)

    assert delivered.response is original
    assert original.choices[0].message.content == "see [2] and [7]"  # type: ignore[union-attr]
    assert delivered.resolution.cited_ids == ["doc:1"]
    assert delivered.resolution.unresolved == (7,)


def test_metadata_adds_the_array_and_leaves_the_text_alone() -> None:
    delivered = deliver(response("see [2] and [7]"), INJECTED, mode=MODE_METADATA)

    body = delivered.response.model_dump(exclude_none=True)
    message = body["choices"][0]["message"]
    assert message["content"] == "see [2] and [7]"
    assert [c["handle"] for c in message["citations"]] == [2]
    assert message["citations_unresolved"] == [7]


def test_footer_strips_and_appends() -> None:
    delivered = deliver(response("see [2] and [7]."), INJECTED, mode=MODE_FOOTER)

    content = delivered.response.choices[0].message.content  # type: ignore[union-attr]
    assert content == "see [2] and.\n\nSources:\n[2] pricing.md"
    assert delivered.resolution.unresolved == (7,)


def test_footer_appends_nothing_when_nothing_was_cited() -> None:
    delivered = deliver(response("no idea"), INJECTED, mode=MODE_FOOTER)

    assert delivered.response.choices[0].message.content == "no idea"  # type: ignore[union-attr]


def test_usage_is_forwarded_as_received_under_footer() -> None:
    """The footer is the gateway's text, not the model's; ``completion_tokens`` stays the
    provider's number."""
    delivered = deliver(response("[1]"), INJECTED, mode=MODE_FOOTER)

    assert delivered.response.usage is not None
    assert delivered.response.usage.completion_tokens == 1


# ---------------------------------------------------------------------------
# streaming delivery
# ---------------------------------------------------------------------------


def frame(content: str | None, *, finish: str | None = None) -> StreamFrame:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": content} if content is not None else {}}],
    }
    if finish is not None:
        payload["choices"][0]["finish_reason"] = finish  # type: ignore[index]
    return StreamFrame(data=json.dumps(payload), chunk=ChatChunk.model_validate(payload))


def run(parts: list[str], *, mode: str) -> tuple[list[StreamFrame], str, StreamCitations]:
    stage = StreamCitations(INJECTED, mode=mode)
    out: list[StreamFrame] = []
    for part in parts:
        out.extend(stage.feed(frame(part)))
    out.extend(stage.feed(frame(None, finish="stop")))
    out.extend(stage.finish())
    text = "".join(
        choice.delta.content or "" for f in out if f.chunk is not None for choice in f.chunk.choices
    )
    return out, text, stage


def test_metadata_passes_every_frame_through_untouched_and_adds_one() -> None:
    inbound = [frame("see ["), frame("2] and"), frame(" [7]")]
    stage = StreamCitations(INJECTED, mode=MODE_METADATA)

    relayed = [f for inbound_frame in inbound for f in stage.feed(inbound_frame)]
    extra = stage.finish()

    assert relayed == inbound  # the same objects, not equal copies
    assert len(extra) == 1
    delta = json.loads(extra[0].data)["choices"][0]["delta"]
    assert "content" not in delta
    assert [c["handle"] for c in delta["citations"]] == [2]
    assert delta["citations_unresolved"] == [7]


def test_off_passes_frames_through_and_adds_nothing() -> None:
    inbound = [frame("see [2]")]
    stage = StreamCitations(INJECTED, mode=MODE_OFF)

    assert [f for i in inbound for f in stage.feed(i)] == inbound
    assert stage.finish() == []
    assert stage.resolution().cited_ids == ["doc:1"]


@pytest.mark.parametrize(
    "parts",
    [
        ["see [7] now"],
        ["see ", "[7] now"],
        ["see [", "7] now"],
        ["see [7", "] now"],
        ["see [7]", " now"],
        ["see", " [", "7", "]", " now"],
        ["s", "e", "e", " ", "[", "7", "]", " ", "n", "o", "w"],
    ],
)
def test_footer_strips_a_hallucinated_handle_over_every_split_point(parts: list[str]) -> None:
    _, text, _ = run(parts, mode=MODE_FOOTER)

    assert text == "see now"


@pytest.mark.parametrize(
    "parts",
    [
        ["see [2] now"],
        ["see [", "2] now"],
        ["see [2", "] now"],
        ["see", " [", "2", "]", " now"],
    ],
)
def test_footer_keeps_a_resolved_handle_over_every_split_point(parts: list[str]) -> None:
    _, text, _ = run(parts, mode=MODE_FOOTER)

    assert text == "see [2] now\n\nSources:\n[2] pricing.md"


def test_footer_forwards_unchanged_frames_as_the_provider_sent_them() -> None:
    """Byte-identity for the common frame: the citation stage only rebuilds a frame whose
    text it changed."""
    inbound = [frame("hello "), frame("world [2]"), frame(".")]
    stage = StreamCitations(INJECTED, mode=MODE_FOOTER)

    relayed = [f for i in inbound for f in stage.feed(i)]

    assert relayed[0] is inbound[0]
    assert relayed[-1] is inbound[-1]


def test_a_literal_bracket_that_never_closes_is_released() -> None:
    _, text, _ = run(["a [", "b] c [x", " and [2"], mode=MODE_FOOTER)

    assert text == "a [b] c [x and [2"


def test_a_frame_that_is_all_tail_is_held_not_emptied() -> None:
    stage = StreamCitations(INJECTED, mode=MODE_FOOTER)

    assert stage.feed(frame("[")) == []
    released = stage.feed(frame("2] ok"))

    assert [c.delta.content for f in released for c in f.chunk.choices] == ["[2] ok"]  # type: ignore[union-attr]


def test_a_frame_carrying_finish_reason_is_never_dropped() -> None:
    stage = StreamCitations(INJECTED, mode=MODE_FOOTER)

    kept = stage.feed(frame("[", finish="stop"))

    assert len(kept) == 1
    assert kept[0].chunk is not None
    assert kept[0].chunk.choices[0].finish_reason == "stop"


def test_code_fences_are_tracked_across_frames() -> None:
    parts = ["Try:\n``", "`\nx = a[", "1]\n```\nthen [", "9]."]

    _, text, _ = run(parts, mode=MODE_FOOTER)

    assert text == "Try:\n```\nx = a[1]\n```\nthen."


def test_an_index_split_across_frames_is_not_stripped() -> None:
    """``arr`` in one frame and ``[0]`` in the next is still an index: the scanner keeps
    the character before a held bracket so the word-boundary rule sees it."""
    _, text, stage = run(["items", "[0] then [2]"], mode=MODE_FOOTER)

    assert text.startswith("items[0] then [2]")
    assert stage.resolution().unresolved == ()


def test_stream_and_whole_text_agree_on_the_record() -> None:
    parts = ["As [", "2] and [1,", " 7] say, ", "not [9]."]

    _, _, stage = run(parts, mode=MODE_FOOTER)
    whole = resolve("".join(parts), INJECTED)

    assert stage.resolution().cited_ids == whole.cited_ids
    assert stage.resolution().unresolved == whole.unresolved


def test_the_added_frames_carry_the_providers_ids() -> None:
    out, _, _ = run(["[1]"], mode=MODE_METADATA)

    added = json.loads(out[-1].data)
    assert added["id"] == "chatcmpl-1"
    assert added["model"] == "m"
    assert added["object"] == "chat.completion.chunk"


def test_unparsed_frames_pass_straight_through() -> None:
    stage = StreamCitations(INJECTED, mode=MODE_FOOTER)
    odd = StreamFrame(data="not json", chunk=None)

    assert stage.feed(odd) == [odd]
