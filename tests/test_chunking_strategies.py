"""``semantic`` and ``sentence_window``, and the invariants every strategy shares.

Every test here runs on :class:`~app.services.tokenizer.WordTokenizer` and on
**hand-written distances**. That is the point of the design task 20 protects: the boundary
signal is computed outside :func:`~app.services.chunking.chunk_document`, so ``semantic``
— the one strategy that needs a network call — is tested with numbers typed into the
source file and no embedder, fake or otherwise.

The distances are chosen against the percentile rather than sprinkled around, because a
nearest-rank breakpoint means a spike only cuts if spikes are *rarer* than the tail the
percentile leaves. Writing "some big numbers" and expecting cuts is how this test file
would end up asserting the opposite of what it says.
"""

from __future__ import annotations

import pytest

from app.schemas.connector_config import CHUNK_STRATEGIES, ChunkingConfig
from app.services.chunking import (
    MAX_SIGNAL_SPANS,
    BoundarySignal,
    MissingBoundarySignal,
    boundary_signal,
    chunk_document,
    needs_signal,
    plan_signal,
)
from app.services.extraction import Extracted, Section
from app.services.tokenizer import Tokenizer, WordTokenizer
from tests.chunking_contract import check_invariants

TOKENIZER: Tokenizer = WordTokenizer()

#: Two subjects, five sentences each. The topic changes exactly once, halfway.
CATS = "Cats purr when they are content. "
ROCKETS = "Rockets burn fuel to reach orbit. "
PLANTED = (CATS * 5 + ROCKETS * 5).strip()


def document(*sections: tuple[str, str | None], atomic: bool = False) -> Extracted:
    return Extracted(
        sections=tuple(Section(text=text, title=title) for text, title in sections),
        atomic_sections=atomic,
    )


def one(text: str, title: str | None = None) -> Extracted:
    return document((text, title))


def config(**settings: object) -> ChunkingConfig:
    # `overlap` defaults to zero, as in `test_chunking.py`: the sizes here are small so the
    # assertions are readable, and SPEC §9.3's real default would fail its own
    # "overlap at most half the chunk size" rule against them.
    return ChunkingConfig.model_validate({"overlap": 0, **settings})


def planted_signal(extracted: Extracted, settings: ChunkingConfig) -> BoundarySignal:
    """A signal whose distance spikes where the subject actually changes.

    Built from the text rather than from an embedder: two spans are "far apart" here
    exactly when one mentions cats and the other rockets. That is what an embedding model
    would say about these sentences, expressed as a fact the test controls.
    """
    request = plan_signal(extracted, settings)
    texts = request.texts
    distances = tuple(
        0.9 if _subject(texts[index]) != _subject(texts[index + 1]) else 0.05
        for index in range(len(texts) - 1)
    )
    return BoundarySignal(spans=request.spans, distances=distances)


def _subject(text: str) -> str:
    return "cats" if "Cats" in text else "rockets"


def flat_signal(
    extracted: Extracted, settings: ChunkingConfig, value: float = 0.4
) -> BoundarySignal:
    """Every gap identical: a document that never changes subject."""
    request = plan_signal(extracted, settings)
    return BoundarySignal(spans=request.spans, distances=(value,) * (len(request.spans) - 1))


def split(extracted: Extracted, settings: ChunkingConfig, **extra: object) -> list[str]:
    chunks = chunk_document(extracted, settings, tokenizer=TOKENIZER, **extra)  # type: ignore[arg-type]
    return [chunk.text for chunk in chunks]


# ---------------------------------------------------------------------------
# the signal is the caller's job
# ---------------------------------------------------------------------------


def test_only_semantic_needs_a_signal() -> None:
    """The question ingestion asks before it starts, and the question task 17's reindexer
    asks to decide between re-embedding a connector and recutting it."""
    assert needs_signal(config(strategy="semantic"))
    assert not any(
        needs_signal(config(strategy=name)) for name in CHUNK_STRATEGIES if name != "semantic"
    )


def test_semantic_without_a_signal_raises_rather_than_falling_back() -> None:
    """A silent fallback to ``recursive`` would mean a connector configured for semantic
    chunking indexing as something else, with nothing on any screen saying so."""
    with pytest.raises(MissingBoundarySignal):
        chunk_document(one(PLANTED), config(strategy="semantic"), tokenizer=TOKENIZER)


def test_a_signal_computed_for_another_document_is_refused() -> None:
    """Offsets into the wrong text would cut at arbitrary points and raise nothing."""
    settings = config(strategy="semantic", chunk_size=200)
    elsewhere = planted_signal(one(PLANTED * 3), settings)

    with pytest.raises(ValueError, match="different text"):
        chunk_document(one("short"), settings, tokenizer=TOKENIZER, signal=elsewhere)


def test_the_planner_and_the_splitter_agree_about_the_spans() -> None:
    """They are the same objects, which is the whole reason ``plan_signal`` hands them
    back through ``boundary_signal`` instead of each side deriving its own."""
    settings = config(strategy="semantic")
    request = plan_signal(one(PLANTED), settings)

    signal = boundary_signal(request, [[1.0, 0.0]] * len(request))

    assert signal.spans is request.spans
    assert len(signal.distances) == len(request.spans) - 1


def test_a_vector_count_that_does_not_match_the_plan_is_refused() -> None:
    settings = config(strategy="semantic")
    request = plan_signal(one(PLANTED), settings)

    with pytest.raises(ValueError, match="were planned"):
        boundary_signal(request, [[1.0, 0.0]])


def test_distance_is_the_same_cosine_the_index_ranks_with() -> None:
    """Orthogonal is 1.0, identical is 0.0, and opposite is 2.0. Getting the sign the
    wrong way round raises nothing and cuts a document where it holds together."""
    request = plan_signal(one("One. Two. Three."), config(strategy="semantic"))

    signal = boundary_signal(request, [[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]])

    assert signal.distances[0] == pytest.approx(1.0), "orthogonal"
    assert signal.distances[1] == pytest.approx(2.0), "opposite"


def test_a_long_document_groups_sentences_instead_of_embedding_every_one() -> None:
    """The cost cap. Past the ceiling the resolution of the boundaries drops; the strategy
    does not silently become a different one."""
    long_document = one("Sentence number one. " * (MAX_SIGNAL_SPANS * 2))

    request = plan_signal(long_document, config(strategy="semantic"))

    assert request.stride > 1
    assert len(request) <= MAX_SIGNAL_SPANS


# ---------------------------------------------------------------------------
# semantic
# ---------------------------------------------------------------------------


def test_semantic_cuts_where_the_subject_changes() -> None:
    """The defining property. ``recursive`` at the same size cuts straight through it."""
    settings = config(strategy="semantic", chunk_size=400, min_chunk_size=0)
    extracted = one(PLANTED)

    chunks = split(extracted, settings, signal=planted_signal(extracted, settings))

    assert len(chunks) == 2
    assert "Rockets" not in chunks[0]
    assert "Cats" not in chunks[1]


def test_recursive_at_the_same_size_does_not() -> None:
    """The comparison the demo is about: without it, "semantic cuts at the topic change"
    is a claim about a splitter nobody has anything to compare against."""
    chunks = split(one(PLANTED), config(strategy="recursive", chunk_size=400))

    assert len(chunks) == 1, "the whole document fits, so recursive never cuts at all"


def test_a_flat_distribution_produces_no_semantic_cut() -> None:
    """A percentile is a statement about *this* document. If every gap is the same size,
    none of them exceeds the breakpoint and the document is correctly left in one piece."""
    settings = config(strategy="semantic", chunk_size=400, min_chunk_size=0)
    extracted = one(PLANTED)

    chunks = split(extracted, settings, signal=flat_signal(extracted, settings))

    assert len(chunks) == 1


def test_the_floor_stops_one_chunk_per_sentence() -> None:
    """A document where every gap spikes. Without the floor this is ``sentence_window``
    without the window, which is worse than either of them."""
    settings = config(strategy="semantic", chunk_size=400, min_chunk_size=40)
    extracted = one(PLANTED)
    request = plan_signal(extracted, settings)
    every_gap = BoundarySignal(
        spans=request.spans,
        # Alternating rather than uniform: uniform would sit *at* the percentile and cut
        # nothing, which would pass this test for the wrong reason.
        distances=tuple(0.9 if index % 2 else 0.05 for index in range(len(request.spans) - 1)),
    )

    chunks = chunk_document(extracted, settings, tokenizer=TOKENIZER, signal=every_gap)

    assert len(chunks) < len(request.spans) // 2
    assert all(chunk.token_count >= 40 for chunk in chunks[:-1])


def test_the_ceiling_still_binds_when_no_boundary_arrives() -> None:
    """``chunk_size`` is a maximum here rather than a target: a semantic chunk runs until
    the next real boundary or until the ceiling, whichever comes first."""
    settings = config(strategy="semantic", chunk_size=60, min_chunk_size=0)
    # Long enough that the ceiling has to fire several times: `PLANTED` alone is about
    # seventy tokens, so one ceiling would end the test with nothing shown.
    extracted = one(" ".join([PLANTED] * 4))

    chunks = chunk_document(
        extracted, settings, tokenizer=TOKENIZER, signal=flat_signal(extracted, settings)
    )

    assert len(chunks) > 2
    assert all(chunk.token_count <= 60 for chunk in chunks)


def test_semantic_keeps_a_chunk_inside_one_page_when_pages_are_atomic() -> None:
    """A PDF page is what a citation names. A chunk spanning four of them can cite at most
    one truthfully, whatever the distances say."""
    settings = config(strategy="semantic", chunk_size=4000, min_chunk_size=0)
    extracted = document(
        ("Page one text here.", "p. 1"), ("Page two text here.", "p. 2"), atomic=True
    )

    chunks = chunk_document(
        extracted, settings, tokenizer=TOKENIZER, signal=flat_signal(extracted, settings)
    )

    assert [chunk.section for chunk in chunks] == ["p. 1", "p. 2"]


def test_semantic_carries_overlap_backwards_like_every_other_strategy() -> None:
    settings = config(strategy="semantic", chunk_size=400, overlap=4, min_chunk_size=0)
    extracted = one(PLANTED)

    chunks = split(extracted, settings, signal=planted_signal(extracted, settings))

    # Four tokens back from the boundary, which with this tokenizer is the tail of the
    # previous sentence — punctuation counts as a token, so it is "they are content." and
    # not four words.
    assert chunks[1].startswith("they are content.")
    assert chunks[0].endswith("they are content.")


# ---------------------------------------------------------------------------
# sentence window
# ---------------------------------------------------------------------------


def test_a_window_embeds_a_sentence_and_returns_its_neighbours() -> None:
    """Small unit to match on, enough context to answer with — and both have to be in the
    chunk, because they are two different strings with two different jobs."""
    chunks = chunk_document(
        one("Alpha one. Bravo two. Charlie three. Delta four. Echo five."),
        config(strategy="sentence_window", window_sentences=1),
        tokenizer=TOKENIZER,
    )

    middle = chunks[2]
    assert middle.embedded_text == "Charlie three."
    assert middle.text == "Bravo two. Charlie three. Delta four."
    assert middle.windowed


def test_the_token_count_is_the_window_not_the_sentence() -> None:
    """``doc_max_tokens`` is measured on this. Counting the sentence instead would let the
    injected context quietly exceed the cap that exists to protect the upstream request."""
    chunks = chunk_document(
        one("Alpha one. Bravo two. Charlie three."),
        config(strategy="sentence_window", window_sentences=1),
        tokenizer=TOKENIZER,
    )

    assert chunks[1].token_count > len(chunks[1].embedded_text.split())


def test_a_window_of_zero_is_the_sentence_itself() -> None:
    chunks = chunk_document(
        one("Alpha one. Bravo two."),
        config(strategy="sentence_window", window_sentences=0),
        tokenizer=TOKENIZER,
    )

    assert [chunk.text for chunk in chunks] == ["Alpha one.", "Bravo two."]
    assert not any(chunk.windowed for chunk in chunks)


def test_a_window_does_not_reach_across_a_section() -> None:
    """Otherwise the chunk's ``page_or_section`` label is true of its first line and of
    nothing else in it."""
    chunks = chunk_document(
        document(("Alpha one. Bravo two.", "One"), ("Charlie three.", "Two")),
        config(strategy="sentence_window", window_sentences=3),
        tokenizer=TOKENIZER,
    )

    last = chunks[-1]
    assert last.section == "Two"
    assert last.text == "Charlie three."


def test_a_sentence_over_the_ceiling_is_split_and_not_windowed() -> None:
    """A window around a piece that is already too big to embed helps nobody."""
    wall = " ".join(f"w{index}" for index in range(200))

    chunks = chunk_document(
        one(wall), config(strategy="sentence_window", chunk_size=50), tokenizer=TOKENIZER
    )

    assert len(chunks) > 1
    assert not any(chunk.windowed for chunk in chunks)


# ---------------------------------------------------------------------------
# the shared invariants
# ---------------------------------------------------------------------------

#: The corpus a splitter goes wrong on. Each of these has broken a real chunker: no
#: punctuation at all, one enormous paragraph, empty sections between full ones, a
#: document that is a single sentence, and a line long enough that walking back to a
#: boundary finds nothing.
PATHOLOGICAL: dict[str, Extracted] = {
    "one long line": one(" ".join(f"w{index}" for index in range(2000))),
    "no punctuation": one("alpha bravo charlie delta echo foxtrot golf hotel " * 40),
    "one huge paragraph": one("word " * 5000),
    "empty sections": document(("real text here.", "One"), ("   ", "Two"), ("more text.", "Three")),
    "a single sentence": one("Just the one sentence."),
    "prose": one(PLANTED),
    "sections": document(
        ("Setup notes here.", "Guide > Setup"), ("Usage notes here.", "Guide > Usage")
    ),
}


@pytest.mark.parametrize("strategy", CHUNK_STRATEGIES)
@pytest.mark.parametrize("corpus", sorted(PATHOLOGICAL), ids=lambda name: name.replace(" ", "_"))
def test_every_strategy_satisfies_the_shared_invariants(strategy: str, corpus: str) -> None:
    """One suite, every strategy, the corpus that breaks splitters.

    A strategy with its own weaker version of "never mid-word" is a strategy nobody would
    notice had one — which is why this is parametrized rather than written out six times.
    """
    extracted = PATHOLOGICAL[corpus]
    settings = config(strategy=strategy, chunk_size=100, min_chunk_size=20)
    signal = flat_signal(extracted, settings) if needs_signal(settings) else None

    chunks = chunk_document(
        extracted,
        settings,
        tokenizer=TOKENIZER,
        # A media type nothing parses structurally, so `code` exercises its fall-back to
        # `recursive` here; `test_chunking_code.py` covers the parsing path.
        media_type="text/plain",
        signal=signal,
    )

    check_invariants(chunks, extracted.text, settings, TOKENIZER, strategy=strategy)


@pytest.mark.parametrize("strategy", CHUNK_STRATEGIES)
def test_every_strategy_survives_an_empty_document(strategy: str) -> None:
    """Not one empty chunk. An empty vector has undefined similarity and would match every
    query that also produced one."""
    extracted = one("   \n\n  ")
    settings = config(strategy=strategy)
    signal = flat_signal(extracted, settings) if needs_signal(settings) else None

    assert chunk_document(extracted, settings, tokenizer=TOKENIZER, signal=signal) == []


@pytest.mark.parametrize("strategy", CHUNK_STRATEGIES)
def test_overlap_that_reaches_back_cannot_stop_progress(strategy: str) -> None:
    """The invariant that keeps a pathological configuration from holding a worker until
    it is killed. The schema refuses overlap over half the size; this asserts the splitter
    does not depend on that being the only guard."""
    extracted = one(" ".join(f"w{index}" for index in range(600)))
    settings = config(strategy=strategy, chunk_size=60, overlap=30, min_chunk_size=0)
    signal = flat_signal(extracted, settings) if needs_signal(settings) else None

    chunks = chunk_document(extracted, settings, tokenizer=TOKENIZER, signal=signal)

    assert 0 < len(chunks) < 600
