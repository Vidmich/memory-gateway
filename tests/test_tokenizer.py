"""Token counting, and the offsets the chunker actually uses.

The offset contract is what everything else rests on, so it is asserted as a *property*
over both implementations rather than as a table of numbers for one: offsets must be
sorted, land on character boundaries, and end at the length of the text. A tokenizer
that broke any of those would not produce wrong chunks — it would produce an
``IndexError`` deep in the splitter, or silently drop the tail of every document.

The tiktoken tests skip when the vocabulary is not available. That is not a gap being
papered over — the fallback exists precisely so ingestion still works there, and
:func:`test_the_fallback_is_used_when_tiktoken_cannot_load` asserts that directly.
"""

from __future__ import annotations

import pytest

from app.services.tokenizer import (
    TiktokenCounter,
    Tokenizer,
    WordTokenizer,
    build_tokenizer,
    count,
    token_index,
    token_span,
)

SAMPLE = "The quick brown fox jumps over the lazy dog. It did not mind."


def tiktoken_or_skip() -> Tokenizer:
    counter = TiktokenCounter()
    if counter.name == "words":
        pytest.skip("the tiktoken vocabulary is not available in this environment")
    return counter


def implementations() -> list[Tokenizer]:
    return [WordTokenizer(), TiktokenCounter()]


@pytest.mark.parametrize("tokenizer", implementations(), ids=lambda t: str(t.name))
@pytest.mark.parametrize(
    "text",
    ["", " ", SAMPLE, "no-spaces-at-all", "日本語のテキスト", "a\n\nb\n\nc", "x" * 5000],
    ids=["empty", "space", "prose", "hyphenated", "japanese", "paragraphs", "long"],
)
def test_offsets_are_a_valid_index_into_the_text(tokenizer: Tokenizer, text: str) -> None:
    offsets = tokenizer.offsets(text)

    assert offsets, "offsets are never empty"
    assert offsets == sorted(offsets)
    assert offsets[-1] == len(text), "the last entry is the end, so the tail is never lost"
    assert all(0 <= offset <= len(text) for offset in offsets)
    # Every entry is a legal slice boundary. Deliberately *not* "the first is zero":
    # leading whitespace is not a token, so text that starts with a space starts its
    # first token after it.
    assert all(text[:offset] is not None for offset in offsets)


@pytest.mark.parametrize("tokenizer", implementations(), ids=lambda t: str(t.name))
def test_the_empty_string_has_no_tokens(tokenizer: Tokenizer) -> None:
    assert count(tokenizer, "") == 0


@pytest.mark.parametrize("tokenizer", implementations(), ids=lambda t: str(t.name))
def test_more_text_never_means_fewer_tokens(tokenizer: Tokenizer) -> None:
    assert count(tokenizer, SAMPLE * 2) >= count(tokenizer, SAMPLE)


def test_the_word_tokenizer_counts_words_and_punctuation() -> None:
    assert count(WordTokenizer(), "hello, world!") == 4  # hello , world !


def test_the_word_tokenizer_is_deterministic() -> None:
    assert WordTokenizer().offsets(SAMPLE) == WordTokenizer().offsets(SAMPLE)


# ---------------------------------------------------------------------------
# spans
# ---------------------------------------------------------------------------


def test_a_span_moves_forward_by_the_number_of_tokens_asked_for() -> None:
    tokenizer = WordTokenizer()
    offsets = tokenizer.offsets(SAMPLE)

    end = token_span(offsets, 0, 4)

    assert count(tokenizer, SAMPLE[:end]) == 4


def test_a_negative_span_moves_backwards() -> None:
    """This is how overlap is expressed: the next chunk begins a fixed number of tokens
    *before* the previous one ended."""
    tokenizer = WordTokenizer()
    offsets = tokenizer.offsets(SAMPLE)
    end = token_span(offsets, 0, 8)

    start = token_span(offsets, end, -3)

    assert count(tokenizer, SAMPLE[start:end]) == 3


def test_a_span_past_the_end_lands_on_the_end() -> None:
    offsets = WordTokenizer().offsets(SAMPLE)

    assert token_span(offsets, 0, 10_000) == len(SAMPLE)


def test_a_span_past_the_beginning_lands_on_zero() -> None:
    """Clamped at both ends, so a caller never has to check whether its overlap ran off
    the front of the document."""
    offsets = WordTokenizer().offsets(SAMPLE)

    assert token_span(offsets, 5, -10_000) == 0


def test_the_token_index_of_the_start_is_zero() -> None:
    assert token_index(WordTokenizer().offsets(SAMPLE), 0) == 0


def test_the_token_index_of_the_end_is_the_token_count() -> None:
    tokenizer = WordTokenizer()
    offsets = tokenizer.offsets(SAMPLE)

    assert token_index(offsets, len(SAMPLE)) == count(tokenizer, SAMPLE)


# ---------------------------------------------------------------------------
# tiktoken
# ---------------------------------------------------------------------------


def test_tiktoken_counts_subwords_not_words() -> None:
    """The reason for having it at all: BPE splits rare and long words, and a chunk sized
    in words is a different size in every language."""
    tokenizer = tiktoken_or_skip()

    assert count(tokenizer, "internationalization") > 1


def test_tiktoken_offsets_land_on_character_boundaries() -> None:
    """BPE works on bytes, so a token boundary can fall inside a multi-byte character.
    Slicing there would raise; the mapping snaps forward instead."""
    tokenizer = tiktoken_or_skip()
    text = "café — 日本語 — naïve"

    for offset in tokenizer.offsets(text):
        text[:offset]  # a slice at a non-character boundary is what this rules out


def test_tiktoken_and_words_agree_on_the_shape_if_not_the_number() -> None:
    """They disagree on counts by design — that is what makes the fallback approximate —
    but both must cover the whole text, or the chunker would lose the tail under one and
    not the other."""
    tokenizer = tiktoken_or_skip()

    assert tokenizer.offsets(SAMPLE)[-1] == WordTokenizer().offsets(SAMPLE)[-1] == len(SAMPLE)


def test_the_fallback_is_used_when_tiktoken_cannot_load() -> None:
    """An air-gapped runner, a cold container with no egress. Chunks come out roughly 30%
    larger than asked for, which costs retrieval quality; raising would cost the feature."""
    counter = TiktokenCounter("a-vocabulary-that-does-not-exist")

    # The name says so (task 101): a document row cut this way must not claim the BPE.
    assert counter.name == "words (a-vocabulary-that-does-not-exist unavailable)"
    assert counter.degraded
    assert counter.offsets(SAMPLE) == WordTokenizer().offsets(SAMPLE)


def test_a_failed_load_is_not_retried_for_every_document() -> None:
    """A network round trip per file would turn a degraded mode into an unusable one."""
    counter = TiktokenCounter("still-not-a-real-vocabulary")
    counter.offsets("first")

    calls: list[str] = []
    original = __import__("tiktoken").get_encoding

    def counted(name: str) -> object:
        calls.append(name)
        return original(name)

    monkey = pytest.MonkeyPatch()
    monkey.setattr("tiktoken.get_encoding", counted)
    try:
        counter.offsets("second")
        counter.offsets("third")
    finally:
        monkey.undo()

    assert calls == []


def test_build_tokenizer_returns_something_usable_either_way() -> None:
    tokenizer = build_tokenizer()

    assert count(tokenizer, SAMPLE) > 0
