"""The four comparison numbers, over a fixture corpus, checked in.

The point of this file is that it is *boring to read and annoying to change*. Chunking
quality has no assertion — there is no expression that says "these boundaries are better" —
so the only defence against a strategy quietly getting worse is a recorded shape that a
change has to visibly rewrite. Without it, "the chunks look about the same" is a feeling,
and a regression in ``semantic`` would ship.

The corpus is three sections about three subjects, sized so the strategies *disagree*.
That is the whole design of the fixture: a corpus where every strategy produced the same
numbers would pin nothing.

Reading the table below:

* ``recursive``, ``fixed`` and ``code`` cut on size, so the three sections become two chunks
  and one of them sits at the ceiling. ``code`` is there because the corpus is Markdown, and
  a code strategy on a file it cannot parse must fall through to ``recursive`` — that it
  matches exactly is the assertion.
* ``by_heading`` and ``semantic`` cut on structure and on meaning respectively, and here they
  agree: three subjects, three chunks, none at the ceiling. They agree *because the fixture
  puts a heading exactly where the subject changes*, which is what makes them different
  strategies on a corpus where it does not.
* ``sentence_window`` produces one chunk per sentence, each windowed, so its numbers are an
  order of magnitude apart from everything else. A change that made them look like the
  others would mean the window had stopped being applied.

If a number here changes, the question is not "update the test" — it is which strategy
moved and whether it moved the right way.
"""

from __future__ import annotations

import pytest

from app.schemas.connector_config import CHUNK_STRATEGIES, ChunkingConfig
from app.services.chunking import (
    BoundarySignal,
    chunk_document,
    needs_signal,
    plan_signal,
)
from app.services.chunking_preview import Distribution, distribution
from app.services.extraction import Extracted, Section
from app.services.tokenizer import Tokenizer, WordTokenizer

TOKENIZER: Tokenizer = WordTokenizer()

EXPENSES = (
    "Expenses are reimbursed within thirty days of submission. "
    "Receipts go through the finance portal, not by email. "
    "Approvals are handled by your line manager. "
) * 5
DEPLOY = (
    "Our deployment runs on Kubernetes in two regions. "
    "Each service declares a readiness probe and a liveness probe. "
    "Rollouts are gradual and always reversible. "
) * 5
SUPPORT = (
    "Support answers within one business day. "
    "Escalations reach the on-call engineer directly. "
    "Every ticket keeps its original thread. "
) * 5

CORPUS = Extracted(
    sections=(
        Section(text=EXPENSES.strip(), title="Handbook > Expenses"),
        Section(text=DEPLOY.strip(), title="Handbook > Deployment"),
        Section(text=SUPPORT.strip(), title="Handbook > Support"),
    )
)

#: 250 tokens against a 390-token corpus, so the ceiling binds for the size-driven
#: strategies and does not for the others. Both halves of that are load-bearing.
SETTINGS = {"chunk_size": 250, "overlap": 0, "min_chunk_size": 40}

#: ``(chunks, min, median, p95, max, at_ceiling, mid_sentence)``.
RECORDED: dict[str, tuple[int, int, int, int, int, int, int]] = {
    "recursive": (2, 140, 140, 250, 250, 1, 0),
    "fixed": (2, 140, 140, 250, 250, 1, 1),
    "by_heading": (3, 115, 135, 140, 140, 0, 0),
    "semantic": (3, 115, 135, 140, 140, 0, 0),
    "sentence_window": (45, 23, 39, 48, 48, 0, 0),
    "code": (2, 140, 140, 250, 250, 1, 0),
}


def _subject(text: str) -> str:
    """Which of the three the fixture's sentences are about.

    The stand-in for an embedding model, and honest about it: the test controls what
    "these two sentences are about different things" means, which is exactly what an
    embedding would tell the splitter and exactly what a fake embedder would obscure.
    """
    if "Expenses" in text or "Receipts" in text or "Approvals" in text:
        return "expenses"
    if "deployment" in text or "service" in text or "Rollouts" in text:
        return "deployment"
    return "support"


def measure(strategy: str) -> Distribution:
    config = ChunkingConfig(strategy=strategy, **SETTINGS)  # type: ignore[arg-type]
    signal = None
    if needs_signal(config):
        request = plan_signal(CORPUS, config)
        texts = request.texts
        signal = BoundarySignal(
            spans=request.spans,
            distances=tuple(
                0.9 if _subject(texts[index]) != _subject(texts[index + 1]) else 0.05
                for index in range(len(texts) - 1)
            ),
        )
    chunks = chunk_document(
        CORPUS,
        config,
        tokenizer=TOKENIZER,
        # Markdown, so `code` exercises its fall-through. A code corpus is
        # `tests/test_chunking_code.py`'s subject.
        media_type="text/markdown",
        signal=signal,
    )
    return distribution(chunks, config)


@pytest.mark.parametrize("strategy", CHUNK_STRATEGIES)
def test_the_recorded_shape_still_holds(strategy: str) -> None:
    found = measure(strategy)

    assert (
        found.chunks,
        found.min_tokens,
        found.median_tokens,
        found.p95_tokens,
        found.max_tokens,
        found.at_ceiling,
        found.mid_sentence,
    ) == RECORDED[strategy]


def test_every_strategy_is_recorded() -> None:
    """A strategy added without a row here would ship with no shape recorded at all, and
    the first change to it would be invisible."""
    assert set(RECORDED) == set(CHUNK_STRATEGIES)


def test_the_fixture_actually_discriminates() -> None:
    """The assertion about the *corpus*. If every strategy produced the same numbers, the
    table above would pin nothing and would still pass every test in this file."""
    shapes = {RECORDED[strategy][:1] + RECORDED[strategy][5:] for strategy in RECORDED}

    assert len(shapes) >= 3, "the fixture no longer tells the strategies apart"


def test_a_code_strategy_on_prose_matches_recursive_exactly() -> None:
    """The degradation, stated as an equality rather than as a hope. A file the strategy
    cannot parse structurally has to come out identical to ``recursive`` — anything else
    means it half-parsed something."""
    assert RECORDED["code"] == RECORDED["recursive"]
