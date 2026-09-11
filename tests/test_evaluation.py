"""The evaluation arithmetic (task 103): scoring, aggregation, dedupe, re-anchoring.

The scoring table is written by hand — recall@k, precision@k and MRR at both granularities,
before and after the budget, including empty relevant sets and negative items — and the
functions have to match it to the decimal. That is the acceptance criterion, and the
reason none of these tests goes near a store.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.services.evaluation import (
    ItemScore,
    Label,
    Retrieved,
    aggregate,
    normalise_question,
    reanchor,
    relevant_documents,
    score_item,
)
from app.services.evaluation_service import parse_question


def hit(chunk: str, document: str, *, injected: bool = True, score: float = 0.5) -> Retrieved:
    return Retrieved(chunk_id=chunk, document_id=document, score=score, injected=injected)


def scored(
    retrieved: Sequence[Retrieved],
    chunks: Iterable[str] = (),
    documents: Iterable[str] = (),
    *,
    k: int = 6,
    **extra: Any,
) -> ItemScore:
    return score_item(
        item_id="i",
        question="q",
        retrieved=retrieved,
        relevant_chunk_ids=chunks,
        relevant_document_ids=documents,
        k=k,
        **extra,
    )


# ---------------------------------------------------------------------------
# one item, by hand
# ---------------------------------------------------------------------------


def test_recall_precision_and_mrr_at_chunk_level() -> None:
    """Six retrieved, relevant at ranks 2 and 5 of three relevant chunks:
    recall 2/3, precision 2/6, MRR 1/2."""
    retrieved = [
        hit("c1", "d1"),
        hit("c2", "d1"),
        hit("c3", "d2"),
        hit("c4", "d2"),
        hit("c5", "d3"),
        hit("c6", "d3"),
    ]

    item = scored(retrieved, chunks=["c2", "c5", "c9"])

    assert item.chunk.recall == 2 / 3
    assert item.chunk.precision == 2 / 6
    assert item.chunk.reciprocal_rank == 1 / 2
    assert item.chunk.first_rank == 2
    assert item.chunk.hit is True


def test_document_level_is_implied_by_the_chunk_labels_and_the_named_documents() -> None:
    labels = (Label("c2", document_id="d1"), Label("c5", document_id="d3"))
    documents = relevant_documents(labels, ["d7"])
    assert documents == {"d1", "d3", "d7"}

    retrieved = [hit("x", "d2"), hit("y", "d3"), hit("z", "d1")]
    item = scored(retrieved, chunks=["c2", "c5"], documents=documents)

    # d3 at rank 2, d1 at rank 3, d7 never: recall 2/3, precision 2/3, MRR 1/2.
    assert item.document.recall == 2 / 3
    assert item.document.precision == 2 / 3
    assert item.document.reciprocal_rank == 1 / 2
    # And chunk-level: neither chunk id came back.
    assert item.chunk.recall == 0.0
    assert item.chunk.reciprocal_rank == 0.0
    assert item.chunk.hit is False


def test_the_budget_column_is_over_the_injected_chunks_only() -> None:
    """A relevant chunk retrieved at rank 3 and dropped by the budget is not a recall."""
    retrieved = [hit("c1", "d1"), hit("c2", "d1"), hit("c3", "d2", injected=False)]

    item = scored(retrieved, chunks=["c3"])

    assert item.chunk.recall == 1.0
    assert item.chunk.first_rank == 3
    assert item.chunk_injected.recall == 0.0
    assert item.chunk_injected.precision == 0.0
    assert item.chunk_injected.reciprocal_rank == 0.0


def test_k_truncates_the_ranking_before_scoring() -> None:
    retrieved = [hit(f"c{i}", "d1") for i in range(1, 9)]

    item = scored(retrieved, chunks=["c7"], k=6)

    assert item.chunk.recall == 0.0
    assert item.chunk.precision == 0.0


def test_precision_is_over_what_was_retrieved_not_over_k() -> None:
    """The floor returned two of a possible six, both relevant: precision 1.0."""
    retrieved = [hit("c1", "d1"), hit("c2", "d1")]

    item = scored(retrieved, chunks=["c1", "c2", "c3"], k=6)

    assert item.chunk.precision == 1.0
    assert item.chunk.recall == 2 / 3


def test_a_negative_item_scores_precision_only() -> None:
    clean = scored([], chunks=[], documents=[])
    dirty = scored([hit("c1", "d1")], chunks=[], documents=[])

    assert clean.negative and dirty.negative
    assert clean.chunk.precision == 1.0
    assert dirty.chunk.precision == 0.0
    assert clean.chunk.recall is None and clean.chunk.reciprocal_rank is None
    assert dirty.document.precision == 0.0


def test_an_item_labelled_at_document_level_only_has_no_chunk_numbers() -> None:
    item = scored([hit("c1", "d1")], chunks=[], documents=["d1"])

    assert item.chunk.recall is None
    assert item.chunk.precision is None
    assert item.document.recall == 1.0
    assert item.document.reciprocal_rank == 1.0


def test_nothing_retrieved_for_a_positive_item_is_zero_everywhere() -> None:
    item = scored([], chunks=["c1"])

    assert item.chunk.recall == 0.0
    assert item.chunk.precision == 0.0
    assert item.chunk.reciprocal_rank == 0.0
    assert item.chunk.hit is False


# ---------------------------------------------------------------------------
# a run, by hand
# ---------------------------------------------------------------------------


def test_the_aggregate_averages_per_item_numbers_over_the_right_populations() -> None:
    items = [
        # Verified: relevant at rank 1 of 1 → recall 1, precision 1/2, MRR 1.
        scored([hit("a", "d1"), hit("b", "d2")], chunks=["a"], documents=["d1"], verified=True),
        # Unverified, generated: relevant at rank 2 of 2 → recall 1/2, precision 1/2, MRR 1/2.
        scored(
            [hit("x", "d3"), hit("y", "d4")],
            chunks=["y", "z"],
            documents=["d4"],
            verified=False,
            source="generated",
        ),
        # Verified negative, clean.
        scored([], chunks=[], verified=True),
        # Document-level only, verified: hit at rank 1.
        scored([hit("m", "d9")], documents=["d9"], verified=True),
    ]

    metrics = aggregate(items, k=6)

    assert metrics.all.items == 4
    assert metrics.all.negatives == 1
    assert metrics.all.negatives_clean == 1
    # Chunk column: two items have chunk labels.
    assert metrics.all.chunk.items == 2
    assert metrics.all.chunk.recall == (1.0 + 0.5) / 2
    assert metrics.all.chunk.mrr == (1.0 + 0.5) / 2
    # Precision averages the two positives and the negative (1.0): (0.5 + 0.5 + 1.0) / 3.
    assert metrics.all.chunk.precision == (0.5 + 0.5 + 1.0) / 3
    # Document column: three items have document labels (the runner derives the two from
    # their chunk labels; here they are passed).
    assert metrics.all.document.items == 3
    assert metrics.all.document.hit_rate == 1.0
    # The verified column leaves the generated item out.
    assert metrics.verified.items == 3
    assert metrics.verified.chunk.items == 1
    assert metrics.verified.chunk.recall == 1.0
    assert metrics.generated == 1
    assert metrics.unverified == 1
    assert metrics.sources == {"manual": 3, "generated": 1}
    assert any("generated by a model" in warning for warning in metrics.warnings)
    assert any("unverified" in warning for warning in metrics.warnings)


def test_a_run_of_hand_checked_items_carries_no_warning() -> None:
    metrics = aggregate([scored([hit("a", "d1")], chunks=["a"], verified=True)], k=6)

    assert metrics.warnings == ()
    assert metrics.all.chunk.recall == 1.0


def test_the_json_shape_is_plain_values() -> None:
    item = scored([hit("a", "d1")], chunks=["a"], verified=True)
    payload = aggregate([item], k=6).as_json()

    assert payload["k"] == 6
    assert payload["all"]["chunk"]["recall"] == 1.0
    assert item.as_json()["retrieved"][0]["chunk_id"] == "a"
    assert item.as_json()["chunk"]["first_rank"] == 1


# ---------------------------------------------------------------------------
# dedupe and re-anchoring
# ---------------------------------------------------------------------------


def test_two_spellings_of_one_question_are_one_key() -> None:
    assert normalise_question("How do I get a refund?") == normalise_question(
        "how do i get a  refund"
    )
    assert normalise_question("Où est le café ?") == normalise_question("ou est le cafe")
    assert normalise_question("A") != normalise_question("B")


PASSAGE = (
    "Everyone gets twenty-five days of annual leave, plus public holidays, and can carry "
    "five days into the next year with their manager's written agreement, which has to be "
    "given before the end of December."
)


def test_a_label_is_re_anchored_to_the_chunk_that_now_contains_its_text() -> None:
    chunks = [("new-0", "Some preamble. " + PASSAGE + " Some more."), ("new-1", "Unrelated text.")]

    assert reanchor(PASSAGE, chunks) == ["new-0"]
    # Whitespace and case do not matter.
    assert reanchor(PASSAGE.upper().replace(" ", "  "), chunks) == ["new-0"]


def test_a_split_passage_re_anchors_to_both_halves_head_first() -> None:
    head, tail = PASSAGE[:110], PASSAGE[110:]
    chunks = [("b", "Later: " + tail), ("a", "Earlier: " + head), ("c", "Nothing to do with it.")]

    assert reanchor(PASSAGE, chunks) == ["a", "b"]


def test_a_passage_that_is_nowhere_re_anchors_to_nothing() -> None:
    chunks = [
        ("a", "The expenses policy: receipts within thirty days."),
        ("b", "Travel is booked centrally."),
    ]

    assert reanchor(PASSAGE, chunks) == []
    assert reanchor("", chunks) == []


def test_a_shared_sentence_is_not_the_passage() -> None:
    """A chunk holding one short clause of the label does not qualify."""
    chunks = [("a", "plus public holidays, and that is all")]

    assert reanchor(PASSAGE, chunks) == []


# ---------------------------------------------------------------------------
# generated questions
# ---------------------------------------------------------------------------


def test_the_generated_question_is_the_first_line_unquoted_with_a_question_mark() -> None:
    assert parse_question('"How many days of leave do I get"\nExplanation...') == (
        "How many days of leave do I get?"
    )
    assert (
        parse_question("Question: What is the expenses deadline?")
        == "What is the expenses deadline?"
    )
    assert parse_question("") is None
    assert parse_question("Sure!") is None
