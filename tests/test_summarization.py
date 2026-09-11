"""Document summarization, the pure half (task 102).

The first test is the one the task said to write first: a summary chunk is never rendered
as a ``source:``. Everything after it is the configuration rule, the fingerprint rule, the
excerpt, the prompt, the reply, and how a summary attaches to a chunk — each a function.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.connector_config import ChunkingConfig, fingerprint
from app.schemas.summarization import (
    SummarizationConfig,
    SummarizationOverride,
    adds_summary_chunk,
    changed_formats,
    context_identity,
    effective,
    prefixes_context,
)
from app.services.chunking import Chunk as CutChunk
from app.services.citations import footer, resolve
from app.services.prompt import render_documents, render_entry
from app.services.retrieval import Chunk, _dedupe
from app.services.summarization import (
    ELISION,
    INSTRUCTION,
    KIND_SOURCE,
    KIND_SUMMARY,
    MAX_SUMMARY_CHARS,
    PROMPT_VERSION,
    SUMMARY_INDEX,
    MalformedSummary,
    build_messages,
    contextual_text,
    embedding_input,
    excerpt,
    parse_summary,
    words_for,
)
from app.services.tokenizer import WordTokenizer
from app.services.vector_store import Match

TOKENIZER = WordTokenizer()
MODEL = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


def retrieved(kind: str, *, index: int = 0, section: str | None = "p. 3") -> Chunk:
    return Chunk(
        id=f"doc:{index}",
        score=0.9,
        text="The expense policy: approvals above the threshold."
        if kind == KIND_SUMMARY
        else "excerpt",
        source_name="handbook.pdf",
        page_or_section=section,
        document_id="11111111-1111-5111-8111-111111111111",
        connector_id="22222222-2222-5222-8222-222222222222",
        chunk_index=index,
        kind=kind,
    )


# ---------------------------------------------------------------------------
# label the summary or do not index it
# ---------------------------------------------------------------------------


def test_a_summary_chunk_is_never_rendered_as_a_source() -> None:
    """The one way this feature could damage the product: a citation to text the document
    does not contain. The heading says what it is, and the page — which a summary has
    none of — is absent."""
    entry = render_entry(3, retrieved(KIND_SUMMARY))

    assert entry.startswith("[3] summary of: handbook.pdf\n")
    assert "source:" not in entry
    assert "p. 3" not in entry
    assert render_entry(1, retrieved(KIND_SOURCE)).startswith("[1] source: handbook.pdf (p. 3)\n")


def test_the_reference_block_mixes_the_two_kinds_by_position() -> None:
    block = render_documents([retrieved(KIND_SOURCE, index=0), retrieved(KIND_SUMMARY, index=-1)])

    assert "[1] source: handbook.pdf" in block
    assert "[2] summary of: handbook.pdf" in block


def test_a_citation_of_a_summary_resolves_as_one_with_no_page() -> None:
    """Task 100's citation carries ``kind`` so a client can label the footer, and the
    section is null: a summary is about the whole document."""
    injected = (retrieved(KIND_SOURCE, index=0), retrieved(KIND_SUMMARY, index=-1))

    resolution = resolve("As [2] says.", injected)

    [citation] = resolution.cited
    payload = citation.as_json(base_url="https://ui")
    assert payload["kind"] == "summary"
    assert payload["section"] is None
    assert footer(resolution.cited).endswith("Sources:\n[2] handbook.pdf (summary)")


def test_a_retrieved_summary_point_reads_as_a_summary_and_drops_its_section() -> None:
    match = Match(
        id="p",
        score=0.8,
        payload={
            "text": "About travel.",
            "source_name": "handbook.pdf",
            "page_or_section": "Summary",
            "chunk_index": SUMMARY_INDEX,
            "kind": "summary",
        },
    )
    chunk = Chunk.of(match)

    assert chunk.is_summary
    assert chunk.page_or_section is None
    assert chunk.as_log_entry(injected=True)["kind"] == "summary"


def test_a_point_written_before_the_key_existed_is_a_source() -> None:
    chunk = Chunk.of(Match(id="p", score=0.8, payload={"text": "old", "source_name": "a.md"}))

    assert chunk.kind == KIND_SOURCE
    assert "kind" not in chunk.as_log_entry(injected=True)


def test_dedupe_keeps_a_summary_beside_its_documents_first_chunk() -> None:
    """The summary's constant index sits one step from chunk zero, and the near-duplicate
    filter must not treat it as an overlapping neighbour: it shares no text with anything."""
    document = str(uuid.uuid4())
    first = Match(id="a", score=0.9, payload={"document_id": document, "chunk_index": 0})
    summary = Match(
        id="s",
        score=0.85,
        payload={"document_id": document, "chunk_index": SUMMARY_INDEX, "kind": "summary"},
    )
    neighbour = Match(id="b", score=0.8, payload={"document_id": document, "chunk_index": 1})

    kept = _dedupe([first, summary, neighbour])

    assert [match.id for match in kept] == ["a", "s"]


def test_dedupe_radius_still_comes_from_window_sentences_alone() -> None:
    """A prefix on the point must not start reading as a radius: ``_radius`` reads
    ``window_sentences`` and nothing else."""
    document = str(uuid.uuid4())
    a = Match(
        id="a", score=0.9, payload={"document_id": document, "chunk_index": 0, "context": "x" * 500}
    )
    b = Match(
        id="b", score=0.8, payload={"document_id": document, "chunk_index": 2, "context": "x" * 500}
    )

    assert [match.id for match in _dedupe([a, b])] == ["a", "b"]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_the_defaults_are_off_and_the_task_s_numbers() -> None:
    config = SummarizationConfig()

    assert config.mode == "off"
    assert config.max_summary_tokens == 150
    assert config.max_input_tokens == 12_000
    assert config.daily_document_cap is None
    assert not config.enabled


@pytest.mark.parametrize("mode", ["off", "summary_chunk", "contextual", "both"])
def test_the_two_questions_a_mode_answers(mode: str) -> None:
    assert adds_summary_chunk(mode) == (mode in ("summary_chunk", "both"))
    assert prefixes_context(mode) == (mode in ("contextual", "both"))


def test_an_unknown_mode_and_an_unknown_format_are_refused() -> None:
    with pytest.raises(ValidationError):
        SummarizationConfig.model_validate({"mode": "sometimes"})
    with pytest.raises(ValidationError, match="not a format this build classifies"):
        SummarizationConfig.model_validate({"overrides": {"pdfs": {"mode": "off"}}})


def test_an_override_resolves_to_a_leaf() -> None:
    """A repository connector wants summaries for the Markdown and not for the lockfiles."""
    config = SummarizationConfig(
        mode="summary_chunk",
        overrides={
            "code": SummarizationOverride(mode="off"),
            "pdf": SummarizationOverride(max_summary_tokens=300),
        },
    )

    assert effective(config, "code").mode == "off"
    assert effective(config, "pdf").mode == "summary_chunk"
    assert effective(config, "pdf").max_summary_tokens == 300
    assert effective(config, "markdown").mode == "summary_chunk"
    assert effective(config, "pdf").overrides == {}
    assert config.enabled


def test_switching_summary_chunk_on_recuts_nothing() -> None:
    """The source chunks are unchanged; one point is added beside them."""
    before = SummarizationConfig()
    after = SummarizationConfig(mode="summary_chunk")

    assert changed_formats(before, after) == frozenset()
    assert changed_formats(after, before) == frozenset()


def test_switching_contextual_on_invalidates_every_format() -> None:
    before = SummarizationConfig()
    after = SummarizationConfig(mode="contextual")

    changed = changed_formats(before, after)

    assert "markdown" in changed and "pdf" in changed and "code" in changed
    assert changed_formats(after, SummarizationConfig(mode="both")) == frozenset()


def test_changing_the_model_under_contextual_is_a_re_embed_and_under_summary_chunk_is_not() -> None:
    contextual = SummarizationConfig(mode="contextual", model_id=MODEL)
    assert changed_formats(contextual, SummarizationConfig(mode="contextual", model_id=OTHER))

    chunked = SummarizationConfig(mode="summary_chunk", model_id=MODEL)
    assert (
        changed_formats(chunked, SummarizationConfig(mode="summary_chunk", model_id=OTHER))
        == frozenset()
    )


def test_an_override_can_turn_contextual_on_for_one_format_only() -> None:
    before = SummarizationConfig()
    after = SummarizationConfig(overrides={"pdf": SummarizationOverride(mode="contextual")})

    assert changed_formats(before, after) == frozenset({"pdf"})


# ---------------------------------------------------------------------------
# the fingerprint rule
# ---------------------------------------------------------------------------


def test_the_fingerprint_folds_context_in_for_contextual_and_not_for_summary_chunk() -> None:
    chunking = ChunkingConfig()
    plain = fingerprint(chunking, tokenizer="words")

    chunk_only = context_identity(
        SummarizationConfig(mode="summary_chunk"), MODEL, prompt_version=1
    )
    assert chunk_only is None
    assert fingerprint(chunking, tokenizer="words", context=chunk_only) == plain

    contextual = context_identity(SummarizationConfig(mode="contextual"), MODEL, prompt_version=1)
    assert contextual is not None
    with_context = fingerprint(chunking, tokenizer="words", context=contextual)
    assert with_context != plain
    # The model's identity and the prompt's version are both part of it.
    assert with_context != fingerprint(
        chunking,
        tokenizer="words",
        context=context_identity(SummarizationConfig(mode="contextual"), OTHER, prompt_version=1),
    )
    assert with_context != fingerprint(
        chunking,
        tokenizer="words",
        context=context_identity(SummarizationConfig(mode="contextual"), MODEL, prompt_version=2),
    )


def test_the_prompt_version_is_the_one_the_code_ships() -> None:
    """A change to the prompt is a change to this number — the test that makes somebody
    read the docstring before bumping it."""
    assert PROMPT_VERSION == 1
    assert "deciding whether to read it" in INSTRUCTION


# ---------------------------------------------------------------------------
# the excerpt
# ---------------------------------------------------------------------------


def test_a_document_that_fits_is_sent_whole() -> None:
    found = excerpt("  one two three  ", TOKENIZER, max_input_tokens=10)

    assert found.text == "one two three"
    assert found.tokens == 3
    assert not found.truncated


def test_a_long_document_is_sent_as_its_head_and_its_tail() -> None:
    """Where an abstract and a conclusion live. The middle is what a summary can afford to
    miss, and the count is what will be paid for."""
    words = [f"w{i}" for i in range(1000)]
    found = excerpt(" ".join(words), TOKENIZER, max_input_tokens=100)

    head, tail = found.text.split(ELISION)
    assert head.split() == words[:75]
    assert tail.split() == words[-25:]
    assert found.tokens == 100
    assert found.truncated


# ---------------------------------------------------------------------------
# the prompt and the reply
# ---------------------------------------------------------------------------


def test_the_prompt_states_the_instruction_before_and_after_the_data() -> None:
    messages = build_messages("Body text.", max_summary_tokens=150, name="handbook.pdf")

    assert [message.role for message in messages] == ["system", "user"]
    body = str(messages[1].content)
    instruction = INSTRUCTION.format(words=words_for(150))
    assert body.count(instruction) == 2
    assert body.index(instruction) < body.index("Body text.") < body.rindex(instruction)
    assert "File name: handbook.pdf" in body


def test_the_delimiter_carries_a_nonce_a_document_cannot_guess() -> None:
    one = str(build_messages("x", max_summary_tokens=150)[1].content)
    two = str(build_messages("x", max_summary_tokens=150)[1].content)

    assert one != two
    assert "<<document " in one and "<</document " in one


def test_words_follow_the_token_budget() -> None:
    assert words_for(150) == 112
    assert words_for(30) == 22


def test_a_reply_is_one_paragraph_with_its_wrappers_removed() -> None:
    assert parse_summary('  "Summary: The handbook,\n  in brief."  ') == "The handbook, in brief."
    assert parse_summary("**Summary:** Plain.") == "Plain."
    assert len(parse_summary("x " * 5000)) <= MAX_SUMMARY_CHARS


def test_an_empty_reply_is_malformed_not_salvaged() -> None:
    with pytest.raises(MalformedSummary):
        parse_summary("   ")
    with pytest.raises(MalformedSummary):
        parse_summary(None)


# ---------------------------------------------------------------------------
# attaching a summary to a chunk
# ---------------------------------------------------------------------------


def test_contextual_text_is_the_summary_a_blank_line_and_the_chunk() -> None:
    assert (
        contextual_text("About travel. ", "The second option.")
        == "About travel.\n\nThe second option."
    )
    assert contextual_text(None, "The second option.") == "The second option."
    assert contextual_text("", "x") == "x"


def test_a_chunk_says_why_its_vector_differs_from_its_text() -> None:
    plain = CutChunk(text="a b c", index=0, section=None, token_count=3)
    windowed = CutChunk(text="a b c", index=0, section=None, token_count=3, embedded_text="b")
    contextual = plain.with_context("S")
    both = windowed.with_context("S")

    assert plain.embedded_because is None and plain.vector_text == "a b c"
    assert windowed.embedded_because == "window" and windowed.vector_text == "b"
    assert contextual.embedded_because == "context" and contextual.vector_text == "S\n\na b c"
    assert both.embedded_because == "window+context" and both.vector_text == "S\n\nb"
    # The returned text never changes.
    assert contextual.text == both.text == "a b c"
    assert plain.with_context(None) is plain


def test_embedding_input_rebuilds_what_a_stored_point_was_embedded_from() -> None:
    """The platform reindex re-embeds stored points; it has to re-embed what the vector
    was made of, prefix and window included, or the new index quietly loses both."""
    assert embedding_input({"text": "whole"}) == "whole"
    assert embedding_input({"text": "whole", "embedded_text": "part"}) == "part"
    assert embedding_input({"text": "whole", "context": "S"}) == "S\n\nwhole"
    assert (
        embedding_input({"text": "whole", "embedded_text": "part", "context": "S"}) == "S\n\npart"
    )
