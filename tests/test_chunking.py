"""Chunking: boundaries, overlap, oversized sections, and token accounting.

Every test here runs on :class:`~app.services.tokenizer.WordTokenizer` rather than
tiktoken, and that is a deliberate choice rather than a convenience. ``tiktoken`` fetches
its vocabulary over the network on first use, so a suite built on it would be a suite that
fails on an air-gapped runner — and, worse, one whose *numbers* nobody could reason about.
With one token per word, "chunk_size=20" means twenty words, and an assertion about a
boundary is an assertion about the splitter instead of about a BPE table.

``tests/test_tokenizer.py`` is where the two are shown to agree.
"""

from __future__ import annotations

import pytest

from app.schemas.connector_config import ChunkingConfig
from app.services.chunking import chunk_document
from app.services.extraction import Extracted, Section
from app.services.tokenizer import Tokenizer, WordTokenizer, count

TOKENIZER: Tokenizer = WordTokenizer()

WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"


def document(*sections: tuple[str, str | None]) -> Extracted:
    return Extracted(sections=tuple(Section(text=text, title=title) for text, title in sections))


def one(text: str, title: str | None = None) -> Extracted:
    return document((text, title))


def split(extracted: Extracted, **settings: object) -> list[str]:
    # `overlap` defaults to zero here, not to SPEC §9.3's 150: the sizes below are small
    # so the assertions are readable, and the real default would fail its own
    # "overlap at most half the chunk size" rule against them.
    config = ChunkingConfig.model_validate({"respect_boundaries": True, "overlap": 0, **settings})
    return [chunk.text for chunk in chunk_document(extracted, config, tokenizer=TOKENIZER)]


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def test_a_short_document_is_one_chunk() -> None:
    assert split(one("just a few words"), chunk_size=50) == ["just a few words"]


def test_an_empty_document_produces_no_chunks() -> None:
    """Not one empty chunk. An empty vector has undefined similarity and would match
    every query that also produced one."""
    assert split(one("   \n\n  ")) == []


def test_no_chunk_exceeds_the_configured_size() -> None:
    """The cap is not a suggestion: a chunk over the embedding model's context is a
    document that can never leave the `embedding` status."""
    text = " ".join(f"word{index}" for index in range(500))

    chunks = split(one(text), chunk_size=60, overlap=10)

    assert chunks
    assert all(count(TOKENIZER, chunk) <= 60 for chunk in chunks)


def test_the_whole_document_is_covered() -> None:
    """Overlap makes chunks share text; nothing may make them *skip* it."""
    words = [f"w{index}" for index in range(300)]

    chunks = split(one(" ".join(words)), chunk_size=50, overlap=8)

    seen = " ".join(chunks)
    assert all(word in seen for word in words)


# ---------------------------------------------------------------------------
# overlap
# ---------------------------------------------------------------------------


def test_consecutive_chunks_overlap() -> None:
    text = " ".join(f"w{index}" for index in range(200))

    chunks = split(one(text), chunk_size=60, overlap=15, respect_boundaries=False)

    first_tail = chunks[0].split()[-15:]
    assert all(word in chunks[1].split() for word in first_tail)


def test_zero_overlap_means_no_shared_words() -> None:
    text = " ".join(f"w{index}" for index in range(120))

    chunks = split(one(text), chunk_size=60, overlap=0, respect_boundaries=False)

    assert set(chunks[0].split()) & set(chunks[1].split()) == set()


def test_each_chunk_starts_after_the_last_one() -> None:
    """The progress invariant. Overlap moves the start back and snapping moves the end
    back; a configuration where those cancelled would loop forever on one document and
    hold a worker until somebody killed it."""
    text = " ".join(f"w{index}" for index in range(400))

    chunks = split(one(text), chunk_size=60, overlap=30)

    starts = [text.index(chunk.split()[0] + " ") for chunk in chunks]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_overlap_may_not_exceed_half_the_chunk_size() -> None:
    """Not an aesthetic limit. At overlap >= size the splitter cannot advance at all, and
    anywhere near it the index is mostly duplicates."""
    with pytest.raises(ValueError, match="at most half"):
        ChunkingConfig(chunk_size=100, overlap=60)


# ---------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------


def test_a_cut_lands_on_a_paragraph_break_when_one_is_near() -> None:
    first = " ".join(["alpha"] * 40)
    second = " ".join(["bravo"] * 40)

    chunks = split(one(f"{first}\n\n{second}"), chunk_size=50, overlap=0)

    assert chunks[0] == first


def test_a_cut_falls_back_to_a_sentence_boundary() -> None:
    sentences = ". ".join(" ".join(["word"] * 12) for _ in range(12)) + "."

    chunks = split(one(sentences), chunk_size=60, overlap=0)

    assert chunks[0].endswith(".")


def test_a_cut_never_lands_mid_word() -> None:
    """The last-resort separator. Source code has no sentences, and half a token embeds
    to nothing while looking fine in a citation."""
    text = " ".join(f"identifier_{index}" for index in range(200))

    chunks = split(one(text), chunk_size=60, overlap=10)

    for chunk in chunks:
        for word in chunk.split():
            assert word in text.split()


def test_a_line_with_no_boundary_at_all_is_cut_anyway() -> None:
    """Minified JSON on one line. There is nowhere better, and refusing to cut would
    produce a chunk the embedding model rejects."""
    text = "x" * 20 + "".join(f"{{k{index}:v{index}}}" for index in range(400))

    chunks = split(one(text), chunk_size=50, overlap=0)

    assert len(chunks) > 1


def test_boundaries_can_be_switched_off() -> None:
    """``respect_boundaries=False`` is exact token windows, which is what ``fixed`` does
    and what content with no meaningful structure wants."""
    text = " ".join(f"w{index}" for index in range(200))

    exact = split(one(text), chunk_size=60, overlap=0, respect_boundaries=False)

    assert all(count(TOKENIZER, chunk) == 60 for chunk in exact[:-1])


def test_fixed_ignores_boundaries_even_when_asked_to_respect_them() -> None:
    """The strategy *is* "cut at the size"; honouring the flag would make ``fixed`` and
    ``recursive`` the same thing under one configuration and not another."""
    first = " ".join(["alpha"] * 40)
    text = f"{first}\n\n" + " ".join(["bravo"] * 40)

    chunks = split(one(text), strategy="fixed", chunk_size=50, overlap=0)

    assert chunks[0] != first


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def test_by_heading_makes_one_chunk_per_section() -> None:
    extracted = document(("Setup text", "Guide > Setup"), ("Usage text", "Guide > Usage"))

    chunks = chunk_document(extracted, ChunkingConfig(strategy="by_heading"), tokenizer=TOKENIZER)

    assert [(chunk.text, chunk.section) for chunk in chunks] == [
        ("Setup text", "Guide > Setup"),
        ("Usage text", "Guide > Usage"),
    ]


def test_by_heading_does_not_merge_small_sections() -> None:
    """The strategy's whole proposition. Gluing three short sections together to hit a
    size target throws away the structure somebody chose it for."""
    extracted = document(("a", "One"), ("b", "Two"), ("c", "Three"))

    chunks = chunk_document(
        extracted, ChunkingConfig(strategy="by_heading", chunk_size=500), tokenizer=TOKENIZER
    )

    assert len(chunks) == 3


def test_an_oversized_section_is_sub_split_and_keeps_its_title() -> None:
    long_text = " ".join(f"w{index}" for index in range(300))
    extracted = document((long_text, "Guide > Appendix"))

    chunks = chunk_document(
        extracted,
        ChunkingConfig(strategy="by_heading", chunk_size=60, overlap=10),
        tokenizer=TOKENIZER,
    )

    assert len(chunks) > 1
    assert {chunk.section for chunk in chunks} == {"Guide > Appendix"}


def test_by_heading_falls_back_to_recursive_for_unstructured_text() -> None:
    """SPEC §9.3's fallback, arrived at by the shape of the data: a format with no
    headings extracts to one section, and one oversized section is sub-split."""
    extracted = one(" ".join(f"w{index}" for index in range(300)), None)

    chunks = chunk_document(
        extracted,
        ChunkingConfig(strategy="by_heading", chunk_size=60, overlap=10),
        tokenizer=TOKENIZER,
    )

    assert len(chunks) > 1
    assert {chunk.section for chunk in chunks} == {None}


def test_recursive_labels_each_chunk_with_the_section_it_starts_in() -> None:
    """Joining the sections is what produces full-size chunks; the label is what keeps a
    citation usable. Losing the second would make ``recursive`` index well and attribute
    badly, for no reason but how the text was assembled."""
    first = " ".join(["alpha"] * 60)
    second = " ".join(["bravo"] * 60)
    extracted = document((first, "One"), (second, "Two"))

    chunks = chunk_document(
        extracted, ChunkingConfig(chunk_size=50, overlap=0), tokenizer=TOKENIZER
    )

    assert chunks[0].section == "One"
    assert chunks[-1].section == "Two"


def test_an_empty_section_is_dropped() -> None:
    extracted = document(("real text", "One"), ("   ", "Two"))

    chunks = chunk_document(extracted, ChunkingConfig(strategy="by_heading"), tokenizer=TOKENIZER)

    assert [chunk.section for chunk in chunks] == ["One"]


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


def test_chunk_indexes_are_contiguous_from_zero() -> None:
    """They are half of the deterministic point id. A gap would leave a stale vector
    behind on the next ingestion."""
    chunks = chunk_document(
        one(" ".join(f"w{index}" for index in range(300))),
        ChunkingConfig(chunk_size=60, overlap=10),
        tokenizer=TOKENIZER,
    )

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_the_reported_token_count_is_the_real_one() -> None:
    chunks = chunk_document(
        one(WORDS), ChunkingConfig(chunk_size=100, overlap=0), tokenizer=TOKENIZER
    )

    assert chunks[0].token_count == count(TOKENIZER, chunks[0].text)
    assert chunks[0].token_count == 10
