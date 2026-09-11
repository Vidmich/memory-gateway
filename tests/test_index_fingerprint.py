"""The index fingerprint (task 104): each input changes it, an unrelated one does not, and
the reason for each single-input change is the right word."""

from __future__ import annotations

import uuid

import pytest

from app.schemas.connector_config import ChunkingConfig, effective
from app.schemas.summarization import ContextIdentity
from app.services.index_fingerprint import (
    CHUNKING,
    EMBEDDING_MODEL,
    EXTRACTOR,
    SUMMARIZATION,
    TOKENIZER,
    UNRECORDED,
    differences,
    index_fingerprint,
    parse,
    reason_sentence,
    stale_reason,
    with_embedding_model,
)


def fingerprint(**changes: object) -> str:
    inputs: dict[str, object] = {
        "chunking": ChunkingConfig(chunk_size=400, overlap=40),
        "embedding_model": "text-embedding-3-small",
        "tokenizer": "o200k_base",
        "context": None,
        "extraction_version": 1,
    }
    inputs.update(changes)
    return index_fingerprint(
        inputs["chunking"],  # type: ignore[arg-type]
        embedding_model=str(inputs["embedding_model"]),
        tokenizer=str(inputs["tokenizer"]),
        context=inputs["context"],  # type: ignore[arg-type]
        extraction_version=int(inputs["extraction_version"]),  # type: ignore[call-overload]
    )


def test_the_fingerprint_is_five_readable_segments() -> None:
    parsed = parse(fingerprint())
    assert parsed is not None
    assert set(parsed) == {"ch", "em", "tk", "sm", "xv"}
    assert parsed["sm"] == "-"
    assert parsed["xv"] == "1"


def test_the_same_inputs_give_the_same_fingerprint() -> None:
    assert fingerprint() == fingerprint()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"chunking": ChunkingConfig(chunk_size=500, overlap=40)}, CHUNKING),
        ({"embedding_model": "text-embedding-3-large"}, EMBEDDING_MODEL),
        ({"tokenizer": "cl100k_base"}, TOKENIZER),
        ({"context": ContextIdentity(model_id=uuid.uuid4(), prompt_version=1)}, SUMMARIZATION),
        ({"extraction_version": 2}, EXTRACTOR),
    ],
)
def test_each_input_changes_it_and_names_itself(change: dict[str, object], reason: str) -> None:
    before = fingerprint()
    after = fingerprint(**change)
    assert before != after
    assert stale_reason(before, after) == reason
    assert differences(before, after) == [reason]


def test_an_unrelated_change_does_not_move_it() -> None:
    """``summary_chunk`` mode adds a point and recuts nothing: its context is ``None``
    just like ``off``, so the fingerprint is the same — the rule task 20 set."""
    assert fingerprint(context=None) == fingerprint()
    # Overrides for a *different* format do not reach this format's effective config.
    with_override = ChunkingConfig.model_validate(
        {"chunk_size": 400, "overlap": 40, "overrides": {"code": {"chunk_size": 900}}}
    )
    assert fingerprint(chunking=effective(with_override, "markdown")) == fingerprint()


def test_two_changes_report_in_priority_order() -> None:
    before = fingerprint()
    after = fingerprint(
        chunking=ChunkingConfig(chunk_size=500, overlap=40), embedding_model="other"
    )
    assert differences(before, after) == [EMBEDDING_MODEL, CHUNKING]
    assert stale_reason(before, after) == EMBEDDING_MODEL


def test_a_blank_or_an_old_digest_is_unrecorded_not_stale() -> None:
    assert parse(None) is None
    assert parse("ab12cd34ef567890") is None
    assert stale_reason(None, fingerprint()) == UNRECORDED
    assert stale_reason("ab12cd34ef567890", fingerprint()) == UNRECORDED
    assert stale_reason(fingerprint(), fingerprint()) is None


def test_the_embedding_segment_can_be_rewritten_alone() -> None:
    """What the platform reindex does to a copied point: one segment moves, the rest is
    byte-identical, and an unreadable value comes back unchanged."""
    before = fingerprint()
    moved = with_embedding_model(before, "text-embedding-3-large")
    assert moved == fingerprint(embedding_model="text-embedding-3-large")
    assert differences(before, moved) == [EMBEDDING_MODEL]
    assert with_embedding_model("ab12cd34ef567890", "anything") == "ab12cd34ef567890"


def test_the_sentence_names_the_old_and_new_values_where_the_row_kept_them() -> None:
    sentence = reason_sentence(
        EMBEDDING_MODEL,
        was={"embedding_model": "text-embedding-3-small"},
        now={"embedding_model": "text-embedding-3-large"},
    )
    assert (
        sentence
        == "Embedded with text-embedding-3-small; the platform is now text-embedding-3-large."
    )
    assert reason_sentence(
        TOKENIZER, was={"tokenizer": "cl100k_base"}, now={"tokenizer": "o200k_base"}
    ) == ("Sized with cl100k_base; the tokenizer is now o200k_base.")
    assert "recursive" in reason_sentence(
        CHUNKING, was={"chunk_strategy": "recursive"}, now={"chunk_strategy": "semantic"}
    )
    assert "settings changed" in reason_sentence(CHUNKING, was={}, now={})
    assert "upgraded" in reason_sentence(EXTRACTOR)
    assert "Not known to be stale" in reason_sentence(UNRECORDED)
