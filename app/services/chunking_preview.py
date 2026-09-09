"""Running candidate chunking configurations over one real document, writing nothing.

**This is not a nice-to-have.** Nobody can pick a chunking strategy from a description:
the right answer depends on the corpus, and the only honest way to choose is to run the
candidates over real documents and look. Without this endpoint task 20 ships three more
words in a dropdown, and every user picks by name — which in practice means picking
``semantic`` because it sounds better, paying for it at every ingestion, and never finding
out whether it helped.

Four properties, and each one exists because its absence would make the answer a lie.

**It is the same code path as ingestion.** The candidates go through the same
:func:`~app.services.chunking.chunk_document` with the same tokenizer and the same signal
computation, so what the screen shows is what the index would hold. A preview that
approximated the splitter would be worse than none: it would be believed.

**It writes nothing.** No vectors, no document row, no chunk. It reads the object, and the
only trace it leaves is what it spends at the embedding provider.

**It says what it spent.** ``embedded_texts`` per candidate is not a diagnostic; it is the
number that answers "what will this cost me every time I ingest a file". A comparison that
showed quality and hid cost would push every user toward the most expensive option.

**Its numbers are the four that make two strategies comparable.** Chunk count, the token
distribution, how many chunks were decided by the size ceiling rather than by meaning, and
how many boundaries fell mid-sentence. A wall of chunk text is not a comparison; nobody
reads two of them side by side and concludes anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.core.errors import Validation
from app.schemas.connector_config import ChunkingConfig, effective
from app.services.chunking import (
    Chunk,
    boundary_signal,
    chunk_document,
    needs_signal,
    plan_signal,
)
from app.services.embeddings import Embedder
from app.services.extraction import Extracted
from app.services.tokenizer import Tokenizer, count
from app.services.vector_store import cosine

#: Candidates per request. Four columns is what fits on a screen side by side, and the
#: cost of this endpoint is linear in it.
MAX_CANDIDATES = 4

#: Chunks returned per candidate. The statistics are computed over *all* of them — a
#: truncated distribution would be a wrong number rather than a short list — and only the
#: rendering is capped.
MAX_CHUNKS_SHOWN = 120

#: A chunk this close to the ceiling was cut by the size limit rather than by a boundary.
#: A fraction rather than equality, because a snapped chunk lands just *under* the ceiling
#: and counting only exact hits would report zero for the strategy that snaps — which is
#: every strategy but ``fixed``.
CEILING_FRACTION = 0.9

#: Sentence-ending punctuation, for the mid-sentence boundary count. Deliberately the same
#: characters the splitter's sentence separator looks for.
_ENDINGS = ".!?\"')]"


class PreviewTooLarge(Validation):
    """The document is too big to preview. A 422 with a number in it, because the fix is
    for the user to pick a smaller document rather than to retry this one."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One configuration to try, with the name the column will carry."""

    label: str
    config: ChunkingConfig


@dataclass(frozen=True, slots=True)
class PreviewChunk:
    index: int
    text: str
    section: str | None
    token_count: int
    #: Set only under ``sentence_window``: the sentence inside ``text`` that was embedded.
    embedded_text: str | None = None
    #: Similarity to the query, when one was given. ``None`` means no query, which is a
    #: different thing from a score of zero and is rendered differently.
    score: float | None = None


@dataclass(frozen=True, slots=True)
class Distribution:
    """The four numbers, plus the two counts. Enough to compare, small enough to read."""

    chunks: int
    min_tokens: int
    median_tokens: int
    p95_tokens: int
    max_tokens: int
    #: Chunks decided by ``chunk_size`` rather than by a boundary. High under ``semantic``
    #: means the ceiling is doing the cutting and the strategy is not earning its cost.
    at_ceiling: int
    #: Boundaries that fell somewhere other than the end of a sentence. The number that
    #: makes ``fixed`` look like what it is.
    mid_sentence: int


@dataclass(frozen=True, slots=True)
class CandidateResult:
    label: str
    strategy: str
    distribution: Distribution
    chunks: tuple[PreviewChunk, ...]
    #: How many chunks exist, when ``chunks`` was truncated for display.
    total_chunks: int
    #: Texts sent to the embedding provider to produce this preview: the sentences a
    #: semantic cut needed, plus the chunks scored against a query. What one ingestion of
    #: this document would cost under this candidate.
    embedded_texts: int
    #: The chunk this candidate would surface for the query, by index. ``None`` with no
    #: query, or when the candidate produced nothing.
    best: int | None = None


@dataclass(frozen=True, slots=True)
class PreviewResult:
    document_id: uuid.UUID
    source_name: str
    media_type: str | None
    #: Which format kind this document resolves under, so the screen can say *why* one
    #: candidate is the connector's effective configuration and another is not.
    format_kind: str
    candidates: tuple[CandidateResult, ...]
    query: str | None = None


class ChunkingPreviewer:
    """Runs candidates over one already-extracted document.

    Takes the extracted text rather than the bytes, because reading and extracting is the
    caller's job and is already written twice over in the pipeline. What is here is only
    the part that is *not* the pipeline: several configurations at once, scored, counted,
    and nothing written down.
    """

    def __init__(self, *, embedder: Embedder, tokenizer: Tokenizer) -> None:
        self._embedder = embedder
        self._tokenizer = tokenizer

    async def run(
        self,
        extracted: Extracted,
        candidates: tuple[Candidate, ...],
        *,
        document_id: uuid.UUID,
        source_name: str,
        media_type: str | None,
        format_kind: str,
        query: str | None = None,
    ) -> PreviewResult:
        if not candidates:
            raise Validation("Give at least one chunking configuration to compare.")
        if len(candidates) > MAX_CANDIDATES:
            raise Validation(
                f"At most {MAX_CANDIDATES} candidates can be compared at once "
                f"({len(candidates)} were given)."
            )

        # Embedded once for every candidate rather than once per candidate: the query is
        # the same question whichever way the document was cut, and re-embedding it would
        # be a cost with no corresponding fact.
        asked = (await self._embedder.embed([query]))[0] if query else None

        results = []
        for candidate in candidates:
            results.append(
                await self._candidate(
                    extracted,
                    candidate,
                    media_type=media_type or "",
                    asked=asked,
                )
            )
        return PreviewResult(
            document_id=document_id,
            source_name=source_name,
            media_type=media_type,
            format_kind=format_kind,
            candidates=tuple(results),
            query=query,
        )

    async def _candidate(
        self,
        extracted: Extracted,
        candidate: Candidate,
        *,
        media_type: str,
        asked: list[float] | None,
    ) -> CandidateResult:
        config = candidate.config
        spent = 0
        signal = None
        if needs_signal(config):
            request = plan_signal(extracted, config)
            spent += len(request)
            signal = boundary_signal(request, await self._embedder.embed(request.texts))

        chunks = chunk_document(
            extracted,
            config,
            tokenizer=self._tokenizer,
            media_type=media_type,
            signal=signal,
        )

        scores: list[float] | None = None
        if asked is not None and chunks:
            vectors = await self._embedder.embed([chunk.embedded_text for chunk in chunks])
            spent += len(vectors)
            # The same cosine the vector store ranks with, not a second similarity that
            # could disagree with what a real request does — which is the whole reason
            # this reuses `vector_store.cosine` rather than defining one here.
            scores = [cosine(asked, vector) for vector in vectors]
        elif chunks:
            # Still what one ingestion costs, even though this preview did not spend it.
            spent += len(chunks)

        best = max(range(len(scores)), key=lambda index: scores[index]) if scores else None
        shown = tuple(
            PreviewChunk(
                index=chunk.index,
                text=chunk.text,
                section=chunk.section,
                token_count=chunk.token_count,
                embedded_text=chunk.embedded_text if chunk.windowed else None,
                score=scores[chunk.index] if scores else None,
            )
            for chunk in chunks[:MAX_CHUNKS_SHOWN]
        )
        return CandidateResult(
            label=candidate.label,
            strategy=config.strategy,
            distribution=distribution(chunks, config),
            chunks=shown,
            total_chunks=len(chunks),
            embedded_texts=spent,
            best=best,
        )


def distribution(chunks: list[Chunk], config: ChunkingConfig) -> Distribution:
    """The four numbers and the two counts, over every chunk rather than the shown ones."""
    if not chunks:
        return Distribution(0, 0, 0, 0, 0, 0, 0)
    sizes = sorted(chunk.token_count for chunk in chunks)
    ceiling = int(config.chunk_size * CEILING_FRACTION)
    return Distribution(
        chunks=len(chunks),
        min_tokens=sizes[0],
        median_tokens=_at(sizes, 50),
        p95_tokens=_at(sizes, 95),
        max_tokens=sizes[-1],
        at_ceiling=sum(1 for size in sizes if size >= ceiling),
        mid_sentence=sum(
            1 for chunk in chunks[:-1] if not chunk.text.rstrip().endswith(tuple(_ENDINGS))
        ),
    )


def _at(ordered: list[int], percentile: int) -> int:
    """Nearest-rank, the same rule the semantic breakpoint uses. One percentile function
    per codebase: two that round differently are two numbers nobody can reconcile."""
    rank = max(1, round(percentile / 100 * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def candidates_from(
    stored: ChunkingConfig, requested: list[dict[str, Any]] | None, *, kind: str
) -> tuple[Candidate, ...]:
    """Build the candidate list, always leading with what the connector does today.

    The current configuration is included whether or not it was asked for, because a
    comparison with no baseline in it is a set of options rather than a decision. It is
    the *effective* one for this document's format — comparing a proposal against the
    connector's top-level setting would be comparing it against something that never
    applies to this file.
    """
    current = Candidate(label="current", config=effective(stored, kind))
    if not requested:
        return (current,)
    proposed = tuple(
        Candidate(
            label=str(entry.get("label") or f"candidate {index + 1}"),
            config=_config_of(entry, current.config, index=index),
        )
        for index, entry in enumerate(requested)
    )
    return (current, *proposed)


def _config_of(entry: dict[str, Any], base: ChunkingConfig, *, index: int) -> ChunkingConfig:
    """One candidate's configuration, filling omitted fields from the current one.

    Partial rather than whole, so "the same but semantic" is one key. Validated through
    the real model, so a candidate the connector could not be saved with is refused here
    rather than previewed and then rejected on save.
    """
    settings = base.model_dump(mode="json")
    settings.pop("overrides", None)
    settings.update({key: value for key, value in entry.items() if key != "label"})
    try:
        return ChunkingConfig.model_validate(settings)
    except Exception as exc:
        raise Validation(str(exc), param=f"candidates.{index}") from exc


def tokens_in(tokenizer: Tokenizer, texts: list[str]) -> int:
    return sum(count(tokenizer, text) for text in texts)


__all__ = [
    "CEILING_FRACTION",
    "MAX_CANDIDATES",
    "MAX_CHUNKS_SHOWN",
    "Candidate",
    "CandidateResult",
    "ChunkingPreviewer",
    "Distribution",
    "PreviewChunk",
    "PreviewResult",
    "PreviewTooLarge",
    "candidates_from",
    "distribution",
    "tokens_in",
]
