"""**Compare**: candidate chunkings over one real document, and the parity that makes it
worth looking at.

The load-bearing test in this file is
:func:`test_a_preview_produces_exactly_what_ingestion_would`. A preview that approximated
the splitter would be worse than no preview at all, because it would be *believed*: the
whole reason the endpoint exists is so somebody picks a strategy with evidence instead of
by name, and evidence that does not match what the pipeline does is a nicer-looking way to
pick wrong.

Everything runs over the memory stack from :mod:`tests.connector_support` — the same
pipeline a worker runs, with four ports swapped — so "what ingestion would do" is measured
by actually ingesting it.
"""

from __future__ import annotations

import pytest

from app.core.errors import Validation
from app.schemas.connector_config import ChunkingConfig, effective
from app.services.chunking import chunk_document
from app.services.chunking_preview import (
    MAX_CANDIDATES,
    Candidate,
    ChunkingPreviewer,
    PreviewTooLarge,
    candidates_from,
    distribution,
)
from app.services.connectors import PREVIEW_MAX_BYTES
from app.services.extraction import Extracted, Section
from app.services.tokenizer import WordTokenizer
from tests.auth_support import make_organization
from tests.connector_support import TOKENIZER, build_connectors

# Two subjects, so a semantic candidate has something to find and a query has a right
# answer. Long enough that a 60-token ceiling produces several chunks either way.
HANDBOOK = (
    "Expenses are reimbursed within thirty days. "
    "Submit receipts through the portal. "
    "Approvals are handled by your manager. "
    "The travel policy covers economy flights only. "
) * 4 + (
    "Our deployment runs on Kubernetes. "
    "Each service has a readiness probe. "
    "Rollouts are gradual and reversible. "
    "Secrets come from the cluster store. "
) * 4


@pytest.fixture
def fixture():  # type: ignore[no-untyped-def]
    return build_connectors(make_organization())


def one(text: str) -> Extracted:
    return Extracted(sections=(Section(text=text),))


# ---------------------------------------------------------------------------
# parity with ingestion
# ---------------------------------------------------------------------------


async def test_a_preview_produces_exactly_what_ingestion_would(fixture) -> None:  # type: ignore[no-untyped-def]
    """The claim the whole feature rests on, verified by ingesting the candidate and
    diffing. If these ever drift, every number on the comparison screen is a guess."""
    await fixture.ingest(("handbook.md", HANDBOOK.encode()))
    document = await fixture.document("handbook.md")
    candidate = {"label": "smaller", "strategy": "recursive", "chunk_size": 60, "overlap": 0}

    preview = await fixture.service.preview_chunking(
        fixture.actor, fixture.connector.id, document.id, candidates=[candidate]
    )

    # Now actually apply it and re-ingest, then compare against what the preview said.
    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        _patch(chunking={"strategy": "recursive", "chunk_size": 60, "overlap": 0}),
    )
    await fixture.service.reindex_connector(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    indexed = await fixture.service.document_chunks(fixture.actor, document.id, limit=200)
    proposed = next(one for one in preview.candidates if one.label == "smaller")
    assert [chunk.text for chunk in proposed.chunks] == [
        chunk.text for chunk in sorted(indexed.chunks, key=lambda chunk: chunk.index)
    ]
    assert proposed.distribution.chunks == len(indexed.chunks)


async def test_the_current_configuration_is_always_a_column(fixture) -> None:  # type: ignore[no-untyped-def]
    """A comparison with no baseline in it is a set of options rather than a decision."""
    await fixture.ingest(("handbook.md", HANDBOOK.encode()))
    document = await fixture.document("handbook.md")

    preview = await fixture.service.preview_chunking(
        fixture.actor, fixture.connector.id, document.id, candidates=[]
    )

    assert [candidate.label for candidate in preview.candidates] == ["current"]


def test_the_baseline_is_the_effective_configuration_for_this_format() -> None:
    """Comparing a proposal against the connector's top-level setting would compare it
    against something that never applies to this file."""
    stored = ChunkingConfig.model_validate(
        {"strategy": "recursive", "overrides": {"code": {"strategy": "code"}}}
    )

    built = candidates_from(stored, None, kind="code")

    assert built[0].config.strategy == "code"
    assert built[0].config == effective(stored, "code")


def test_a_candidate_is_a_partial_over_that_baseline() -> None:
    """ "The same but semantic" should be one key, not a whole configuration retyped."""
    stored = ChunkingConfig.model_validate({"chunk_size": 700, "overlap": 50})

    built = candidates_from(stored, [{"strategy": "semantic"}], kind="text")

    assert built[1].config.strategy == "semantic"
    assert built[1].config.chunk_size == 700


def test_a_candidate_the_connector_could_not_be_saved_with_is_refused() -> None:
    """Refused here rather than previewed and then rejected on save, which would show
    somebody a comparison they cannot act on."""
    with pytest.raises(Validation, match="at most half") as raised:
        candidates_from(ChunkingConfig(), [{"chunk_size": 60, "overlap": 500}], kind="text")

    # The param names the column, so the form can put the message on the right one.
    assert raised.value.param == "candidates.0"


# ---------------------------------------------------------------------------
# the four numbers
# ---------------------------------------------------------------------------


def test_the_distribution_is_computed_over_every_chunk_not_the_shown_ones() -> None:
    """A truncated distribution would be a wrong number rather than a short list."""
    settings = ChunkingConfig(strategy="recursive", chunk_size=60, overlap=0)
    chunks = chunk_document(one(HANDBOOK * 20), settings, tokenizer=WordTokenizer())

    found = distribution(chunks, settings)

    assert found.chunks == len(chunks)
    assert found.min_tokens <= found.median_tokens <= found.p95_tokens <= found.max_tokens


def test_fixed_reports_the_mid_sentence_boundaries_it_produces() -> None:
    """The number that makes ``fixed`` look like what it is, next to a strategy that snaps."""
    fixed = ChunkingConfig(strategy="fixed", chunk_size=60, overlap=0)
    snapping = ChunkingConfig(strategy="recursive", chunk_size=60, overlap=0)

    blunt = distribution(chunk_document(one(HANDBOOK), fixed, tokenizer=TOKENIZER), fixed)
    careful = distribution(chunk_document(one(HANDBOOK), snapping, tokenizer=TOKENIZER), snapping)

    assert blunt.mid_sentence > careful.mid_sentence


def test_at_ceiling_counts_the_chunks_the_size_limit_decided() -> None:
    """High under ``semantic`` means the ceiling is doing the cutting and the strategy is
    not earning what it costs — which is exactly what somebody comparing needs to see."""
    settings = ChunkingConfig(strategy="fixed", chunk_size=60, overlap=0)

    found = distribution(chunk_document(one(HANDBOOK), settings, tokenizer=TOKENIZER), settings)

    assert found.at_ceiling >= found.chunks - 1, "every chunk but the last is a full window"


def test_an_empty_document_has_an_empty_distribution_rather_than_a_crash() -> None:
    assert distribution([], ChunkingConfig()).chunks == 0


# ---------------------------------------------------------------------------
# cost, queries and refusals
# ---------------------------------------------------------------------------


async def test_each_candidate_reports_what_one_ingestion_would_cost(fixture) -> None:  # type: ignore[no-untyped-def]
    """Reported beside the quality numbers on purpose. A comparison that showed quality and
    hid cost would push every reader toward the most expensive option."""
    previewer = ChunkingPreviewer(embedder=fixture.embedder, tokenizer=TOKENIZER)
    recursive = ChunkingConfig(strategy="recursive", chunk_size=60, overlap=0)
    semantic = ChunkingConfig(strategy="semantic", chunk_size=60, overlap=0, min_chunk_size=0)

    result = await previewer.run(
        one(HANDBOOK),
        (Candidate("current", recursive), Candidate("semantic", semantic)),
        document_id=fixture.connector.id,
        source_name="handbook.md",
        media_type="text/markdown",
        format_kind="markdown",
    )

    cheap, dear = result.candidates
    assert dear.embedded_texts > cheap.embedded_texts, (
        "semantic embeds every sentence as well as every chunk"
    )


async def test_a_query_names_the_chunk_each_candidate_would_surface(fixture) -> None:  # type: ignore[no-untyped-def]
    """The question anybody comparing chunkings is actually asking, answered with the same
    cosine the vector store ranks with rather than a second scoring path."""
    previewer = ChunkingPreviewer(embedder=fixture.embedder, tokenizer=TOKENIZER)
    settings = ChunkingConfig(strategy="recursive", chunk_size=60, overlap=0)

    result = await previewer.run(
        one(HANDBOOK),
        (Candidate("current", settings),),
        document_id=fixture.connector.id,
        source_name="handbook.md",
        media_type="text/markdown",
        format_kind="markdown",
        query="how are rollouts done on the cluster",
    )

    candidate = result.candidates[0]
    assert candidate.best is not None
    assert all(chunk.score is not None for chunk in candidate.chunks)
    assert "Rollouts" in candidate.chunks[candidate.best].text


async def test_no_query_means_no_score_rather_than_a_score_of_zero(fixture) -> None:  # type: ignore[no-untyped-def]
    previewer = ChunkingPreviewer(embedder=fixture.embedder, tokenizer=TOKENIZER)

    result = await previewer.run(
        one(HANDBOOK),
        (Candidate("current", ChunkingConfig(strategy="recursive", chunk_size=60, overlap=0)),),
        document_id=fixture.connector.id,
        source_name="handbook.md",
        media_type="text/markdown",
        format_kind="markdown",
    )

    assert result.candidates[0].best is None
    assert all(chunk.score is None for chunk in result.candidates[0].chunks)


async def test_too_many_candidates_is_refused(fixture) -> None:  # type: ignore[no-untyped-def]
    previewer = ChunkingPreviewer(embedder=fixture.embedder, tokenizer=TOKENIZER)
    many = tuple(Candidate(f"c{index}", ChunkingConfig()) for index in range(MAX_CANDIDATES + 1))

    with pytest.raises(Validation, match="candidates can be compared"):
        await previewer.run(
            one("text"),
            many,
            document_id=fixture.connector.id,
            source_name="a.md",
            media_type="text/markdown",
            format_kind="markdown",
        )


async def test_a_large_document_is_refused_before_anything_is_embedded(fixture) -> None:  # type: ignore[no-untyped-def]
    """This endpoint spends money on every call and stores none of it, so the ceiling is
    the size of a document somebody can read the chunks of, not the size this product can
    index."""
    await fixture.ingest(("handbook.md", HANDBOOK.encode()))
    document = await fixture.document("handbook.md")
    document.size_bytes = PREVIEW_MAX_BYTES + 1

    with pytest.raises(PreviewTooLarge):
        await fixture.service.preview_chunking(fixture.actor, fixture.connector.id, document.id)


async def test_a_preview_writes_nothing(fixture) -> None:  # type: ignore[no-untyped-def]
    """No vectors, no rows. The only trace it leaves is what it spent at the provider."""
    await fixture.ingest(("handbook.md", HANDBOOK.encode()))
    document = await fixture.document("handbook.md")
    before = await fixture.chunk_count()

    await fixture.service.preview_chunking(
        fixture.actor,
        fixture.connector.id,
        document.id,
        candidates=[{"strategy": "recursive", "chunk_size": 60, "overlap": 0}],
        query="expenses",
    )

    assert await fixture.chunk_count() == before
    assert (await fixture.document("handbook.md")).status == "indexed"


def _patch(**fields: object):  # type: ignore[no-untyped-def]
    from app.services.connectors import ConnectorPatch

    return ConnectorPatch(**fields)  # type: ignore[arg-type]
