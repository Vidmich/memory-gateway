"""The arithmetic of a retrieval evaluation (task 103, SPEC §6.6). Pure.

An evaluation set is labelled questions; a run retrieves for each one and scores what came
back against the labels. Everything that is a *function* of those two lists is here —
scoring one item, aggregating a run, telling a duplicate question from a new one, and
re-anchoring a chunk label after a recut — and nothing that touches a store or an index
is, so the numbers can be checked by hand against a table.

Three decisions the numbers depend on, stated once.

**Precision is over what was retrieved, not over ``k``.** ``doc_min_score`` returning three
chunks when ``doc_top_k`` is six is the setting doing its job, and dividing by six would
punish exactly the restraint the knob exists to buy. So precision@k is relevant retrieved
over retrieved (at most ``k``), and a negative item — a question with no relevant chunk —
scores precision 1.0 when nothing came back and 0.0 when something did. That is the only
number a negative item contributes to; it has nothing to recall and nothing to rank.

**Two granularities, both reported.** A person can usually say which *document* answers a
question and rarely which chunk. An item with chunk labels also has document labels (the
chunks' documents, plus any it names directly); an item with only document labels has no
chunk-level number and is left out of that column rather than scored zero in it. The
report shows both columns, and says how many items each is over.

**Before and after the budget.** ``retrieved`` is everything the store returned above the
floor; ``injected`` is what survived ``doc_max_tokens``. A relevant chunk retrieved at rank
five and dropped by the budget is not a recall, and the "after" columns are the ones that
describe what the model would actually have seen.
"""

from __future__ import annotations

import difflib
import re
import statistics
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.index_audit import text_hash

#: Items per run. Above this a set is a batch, not a click; the run says so and takes the
#: first N in the set's order, and the screen says how many it left out.
MAX_ITEMS_PER_RUN = 500

SOURCE_LOG = "log"
SOURCE_CITATION = "citation"
SOURCE_MANUAL = "manual"
SOURCE_GENERATED = "generated"

#: Re-anchoring: a chunk that contains this much of a label's text, contiguously, is the
#: chunk the label meant — or one of them, when a recut split the text in two.
REANCHOR_MIN_CHARS = 60
REANCHOR_MIN_SHARE = 0.5

_SPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Label:
    """One chunk somebody said answers the question, with the text it had at the time."""

    chunk_id: str
    document_id: str | None = None
    source_name: str | None = None
    text: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "source_name": self.source_name,
            "text": self.text,
        }

    @classmethod
    def of(cls, value: Mapping[str, Any]) -> Label:
        return cls(
            chunk_id=str(value.get("chunk_id", "")),
            document_id=_optional(value.get("document_id")),
            source_name=_optional(value.get("source_name")),
            text=_optional(value.get("text")),
        )


def labels_of(values: Iterable[Mapping[str, Any]]) -> tuple[Label, ...]:
    return tuple(Label.of(value) for value in values if value.get("chunk_id"))


def relevant_documents(labels: Sequence[Label], document_ids: Iterable[str]) -> frozenset[str]:
    """The document-level truth: the chunks' documents plus the ones named directly."""
    return frozenset(
        {label.document_id for label in labels if label.document_id} | set(document_ids)
    )


def normalise_question(text: str) -> str:
    """The key two questions are the same question under: case, punctuation, spacing and
    accents folded away. "How do I get a refund?" and "how do i get a refund" are one item;
    a set with both would count one retrieval failure twice."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _SPACE.sub(" ", _PUNCTUATION.sub(" ", stripped)).strip()


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retrieved:
    """One chunk a run got back, in rank order."""

    chunk_id: str
    document_id: str | None
    score: float
    injected: bool
    source_name: str = ""
    page_or_section: str | None = None
    chunk_index: int = 0
    text: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "score": round(self.score, 6),
            "injected": self.injected,
            "source_name": self.source_name,
            "page_or_section": self.page_or_section,
            "chunk_index": self.chunk_index,
        }


@dataclass(frozen=True, slots=True)
class Level:
    """The numbers at one granularity, over one list (before or after the budget).

    ``None`` means "this item has nothing to say here": no chunk labels, so no chunk-level
    recall; no relevant thing at all, so no rank to reciprocate. Never zero in disguise.
    """

    recall: float | None
    precision: float | None
    reciprocal_rank: float | None
    hit: bool | None
    first_rank: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            "recall": self.recall,
            "precision": self.precision,
            "reciprocal_rank": self.reciprocal_rank,
            "hit": self.hit,
            "first_rank": self.first_rank,
        }


@dataclass(frozen=True, slots=True)
class ItemScore:
    item_id: str
    question: str
    source: str
    verified: bool
    negative: bool
    retrieved: tuple[Retrieved, ...]
    relevant_chunk_ids: frozenset[str]
    relevant_document_ids: frozenset[str]
    chunk: Level
    chunk_injected: Level
    document: Level
    document_injected: Level
    #: Chunk labels the run could not find in the index and could not re-anchor by text.
    unanchored: int = 0
    #: Chunk labels that were re-anchored to a new id by their text.
    reanchored: int = 0
    outcome: str = "hit"
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "question": self.question,
            "source": self.source,
            "verified": self.verified,
            "negative": self.negative,
            "retrieved": [entry.as_json() for entry in self.retrieved],
            "relevant_chunk_ids": sorted(self.relevant_chunk_ids),
            "relevant_document_ids": sorted(self.relevant_document_ids),
            "chunk": self.chunk.as_json(),
            "chunk_injected": self.chunk_injected.as_json(),
            "document": self.document.as_json(),
            "document_injected": self.document_injected.as_json(),
            "unanchored": self.unanchored,
            "reanchored": self.reanchored,
            "outcome": self.outcome,
            "error": self.error,
        }


def score_item(
    *,
    item_id: str,
    question: str,
    retrieved: Sequence[Retrieved],
    relevant_chunk_ids: Iterable[str],
    relevant_document_ids: Iterable[str],
    k: int,
    source: str = SOURCE_MANUAL,
    verified: bool = False,
    unanchored: int = 0,
    reanchored: int = 0,
    outcome: str = "hit",
    error: str | None = None,
) -> ItemScore:
    """One item, at both granularities, before and after the budget."""
    chunks = frozenset(relevant_chunk_ids)
    documents = frozenset(relevant_document_ids)
    negative = not chunks and not documents
    ranked = tuple(retrieved[:k])
    injected = tuple(entry for entry in ranked if entry.injected)
    return ItemScore(
        item_id=item_id,
        question=question,
        source=source,
        verified=verified,
        negative=negative,
        retrieved=tuple(retrieved),
        relevant_chunk_ids=chunks,
        relevant_document_ids=documents,
        chunk=_level([e.chunk_id for e in ranked], chunks, negative=negative),
        chunk_injected=_level([e.chunk_id for e in injected], chunks, negative=negative),
        document=_level([e.document_id or "" for e in ranked], documents, negative=negative),
        document_injected=_level(
            [e.document_id or "" for e in injected], documents, negative=negative
        ),
        unanchored=unanchored,
        reanchored=reanchored,
        outcome=outcome,
        error=error,
    )


def _level(ranked: Sequence[str], relevant: frozenset[str], *, negative: bool) -> Level:
    if negative:
        # The only thing a negative item can be wrong about is returning something.
        return Level(
            recall=None,
            precision=1.0 if not ranked else 0.0,
            reciprocal_rank=None,
            hit=None,
            first_rank=None,
        )
    if not relevant:
        # Labelled at the other granularity only. Nothing to say at this one.
        return Level(recall=None, precision=None, reciprocal_rank=None, hit=None, first_rank=None)
    hits = [index for index, identifier in enumerate(ranked, start=1) if identifier in relevant]
    found = {identifier for identifier in ranked if identifier in relevant}
    first = hits[0] if hits else None
    return Level(
        recall=len(found) / len(relevant),
        precision=(len(hits) / len(ranked)) if ranked else 0.0,
        reciprocal_rank=(1.0 / first) if first else 0.0,
        hit=bool(hits),
        first_rank=first,
    )


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One granularity's headline numbers over a population of items."""

    items: int
    recall: float | None
    precision: float | None
    mrr: float | None
    hit_rate: float | None

    def as_json(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "recall": self.recall,
            "precision": self.precision,
            "mrr": self.mrr,
            "hit_rate": self.hit_rate,
        }


@dataclass(frozen=True, slots=True)
class Population:
    """All four columns for one set of items: two granularities by before/after budget."""

    items: int
    negatives: int
    #: Negatives for which nothing was retrieved: the floor held.
    negatives_clean: int
    chunk: Column
    chunk_injected: Column
    document: Column
    document_injected: Column

    def as_json(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "negatives": self.negatives,
            "negatives_clean": self.negatives_clean,
            "chunk": self.chunk.as_json(),
            "chunk_injected": self.chunk_injected.as_json(),
            "document": self.document.as_json(),
            "document_injected": self.document_injected.as_json(),
        }


@dataclass(frozen=True, slots=True)
class RunMetrics:
    k: int
    all: Population
    verified: Population
    #: Source counts, so the headline can say "12 of these labels were written by a model".
    sources: Mapping[str, int]
    generated: int
    unverified: int
    unanchored: int
    reanchored: int
    failed: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "all": self.all.as_json(),
            "verified": self.verified.as_json(),
            "sources": dict(self.sources),
            "generated": self.generated,
            "unverified": self.unverified,
            "unanchored": self.unanchored,
            "reanchored": self.reanchored,
            "failed": self.failed,
            "warnings": list(self.warnings),
        }


def aggregate(items: Sequence[ItemScore], *, k: int) -> RunMetrics:
    """The run's headline, over every item and over the verified ones only."""
    sources: dict[str, int] = {}
    for item in items:
        sources[item.source] = sources.get(item.source, 0) + 1
    generated = sources.get(SOURCE_GENERATED, 0)
    unverified = sum(1 for item in items if not item.verified)
    warnings = []
    if generated:
        warnings.append(
            f"{generated} of {len(items)} items were generated by a model from the chunk that "
            "labels them. Synthetic questions over-estimate recall: the question was written "
            "from the answer."
        )
    if unverified:
        warnings.append(
            f"{unverified} items are unverified — imported from the log or generated, and "
            "not yet confirmed by a person. The verified column is over the rest."
        )
    failed = sum(1 for item in items if item.outcome not in ("hit", "empty"))
    if failed:
        warnings.append(f"{failed} items could not be retrieved for; they are scored as misses.")
    return RunMetrics(
        k=k,
        all=_population(items),
        verified=_population([item for item in items if item.verified]),
        sources=sources,
        generated=generated,
        unverified=unverified,
        unanchored=sum(item.unanchored for item in items),
        reanchored=sum(item.reanchored for item in items),
        failed=failed,
        warnings=tuple(warnings),
    )


def _population(items: Sequence[ItemScore]) -> Population:
    negatives = [item for item in items if item.negative]
    return Population(
        items=len(items),
        negatives=len(negatives),
        negatives_clean=sum(1 for item in negatives if item.chunk.precision == 1.0),
        chunk=_column([item.chunk for item in items]),
        chunk_injected=_column([item.chunk_injected for item in items]),
        document=_column([item.document for item in items]),
        document_injected=_column([item.document_injected for item in items]),
    )


def _column(levels: Sequence[Level]) -> Column:
    recalls = [level.recall for level in levels if level.recall is not None]
    precisions = [level.precision for level in levels if level.precision is not None]
    ranks = [level.reciprocal_rank for level in levels if level.reciprocal_rank is not None]
    hits = [level.hit for level in levels if level.hit is not None]
    return Column(
        # The column's population is the items with a recall — the positives labelled at
        # this granularity. Precision is over one more set (the negatives), and the
        # negatives count is on the population.
        items=len(recalls),
        recall=statistics.fmean(recalls) if recalls else None,
        precision=statistics.fmean(precisions) if precisions else None,
        mrr=statistics.fmean(ranks) if ranks else None,
        hit_rate=(sum(1 for hit in hits if hit) / len(hits)) if hits else None,
    )


# ---------------------------------------------------------------------------
# re-anchoring
# ---------------------------------------------------------------------------


def reanchor(text: str, chunks: Sequence[tuple[str, str]]) -> list[str]:
    """The ids of the chunks that now hold a label's text, after a recut.

    ``chunks`` are ``(id, text)`` for the document the label pointed at. Exact containment
    wins, and one id comes back. When the recut split the text, the chunks that each hold a
    substantial contiguous part of it come back — both halves are where the answer now
    lives, and a retrieval that finds either has found it. Nothing that holds less than
    :data:`REANCHOR_MIN_CHARS` (or half the label, for short labels) qualifies: a shared
    sentence is not the passage.
    """
    wanted = _fold(text)
    if not wanted:
        return []
    exact = [identifier for identifier, body in chunks if wanted in _fold(body)]
    if exact:
        return exact[:1]
    threshold = min(REANCHOR_MIN_CHARS, max(1, int(len(wanted) * REANCHOR_MIN_SHARE)))
    holders = []
    for identifier, body in chunks:
        folded = _fold(body)
        if not folded:
            continue
        matcher = difflib.SequenceMatcher(None, wanted, folded, autojunk=False)
        match = matcher.find_longest_match(0, len(wanted), 0, len(folded))
        if match.size >= threshold:
            holders.append((match.a, identifier))
    # In the order the parts appear in the label, so the first id is the head.
    return [identifier for _, identifier in sorted(holders)]


def same_text(left: str, right: str) -> bool:
    return text_hash(left) == text_hash(right)


def _fold(text: str) -> str:
    return _SPACE.sub(" ", text).strip().casefold()


def _optional(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


__all__ = [
    "MAX_ITEMS_PER_RUN",
    "SOURCE_CITATION",
    "SOURCE_GENERATED",
    "SOURCE_LOG",
    "SOURCE_MANUAL",
    "Column",
    "ItemScore",
    "Label",
    "Level",
    "Population",
    "Retrieved",
    "RunMetrics",
    "aggregate",
    "labels_of",
    "normalise_question",
    "reanchor",
    "relevant_documents",
    "same_text",
    "score_item",
]
