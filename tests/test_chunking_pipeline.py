"""Chunking as a *pipeline step*: per-format resolution, provenance, and the new failure.

``chunking`` was a pure CPU step for the whole of this product's life. Under ``semantic``
it makes an embedding call per sentence, and that changes three things at once: it can
fail, it costs money, and it is no longer instant. Each of those has a test here.

The one worth reading first is
:func:`test_a_provider_outage_during_chunking_names_the_provider`.
The pipeline's whole failure design is "is the document bad, or is the world bad", and a
new step that can fail is a new chance to get that backwards — one way a provider blip
permanently fails a thousand documents, the other way a corrupt file is retried until it
dead-letters.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from prometheus_client import CollectorRegistry

from app.core.metrics import build_chunking_metrics
from app.services.connectors import ConnectorPatch
from app.services.embeddings import EmbeddingError, HashEmbedder
from tests.auth_support import make_organization
from tests.connector_support import DIMENSION, build_connectors

MARKDOWN = (
    "Expenses are reimbursed within thirty days. Receipts go through the portal. " * 8
).strip()

PYTHON = '''import os


def alpha(value):
    """Add one."""
    return value + 1


def bravo(value):
    """Take one away."""
    return value - 1
'''


class Refusing(HashEmbedder):
    """An embedder that fails for a while and then recovers.

    Subclasses the real hashing one rather than replacing it, so everything it is *not*
    refusing still produces usable vectors and the retry actually indexes something.
    """

    def __init__(self, *, retryable: bool, failures: int = 1_000_000) -> None:
        super().__init__(dimension=DIMENSION, model="hash-bow")
        self.retryable = retryable
        self.remaining = failures
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise EmbeddingError(
                "The embedding provider returned 400: model not found",
                retryable=self.retryable,
            )
        return await super().embed(texts)


@pytest.fixture
def fixture():  # type: ignore[no-untyped-def]
    return build_connectors(make_organization())


async def semantic(built) -> None:  # type: ignore[no-untyped-def]
    await built.service.update_connector(
        built.actor,
        built.connector.id,
        ConnectorPatch(chunking={"strategy": "semantic", "chunk_size": 60, "overlap": 0}),
    )


# ---------------------------------------------------------------------------
# per-format resolution
# ---------------------------------------------------------------------------


async def test_each_format_is_cut_with_its_own_effective_configuration(fixture) -> None:  # type: ignore[no-untyped-def]
    """The whole reason a connector is a source rather than a format. One upload of each,
    one connector, two strategies."""
    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(
            chunking={
                "strategy": "recursive",
                "chunk_size": 60,
                "overlap": 0,
                "overrides": {"code": {"strategy": "code"}},
            }
        ),
    )

    await fixture.ingest(("handbook.md", MARKDOWN.encode()), ("util.py", PYTHON.encode()))

    assert (await fixture.document("handbook.md")).chunk_strategy == "recursive"
    assert (await fixture.document("util.py")).chunk_strategy == "code"


async def test_a_code_document_is_cited_by_its_function(fixture) -> None:  # type: ignore[no-untyped-def]
    """The demo's claim: the citation names the function, not lines 40-80 of a file."""
    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"strategy": "code", "chunk_size": 200, "overlap": 0}),
    )

    await fixture.ingest(("util.py", PYTHON.encode()))

    document = await fixture.document("util.py")
    found = await fixture.service.document_chunks(fixture.actor, document.id)
    assert {chunk.payload.get("page_or_section") for chunk in found.chunks} >= {"alpha", "bravo"}


async def test_changing_one_override_reindexes_only_that_format(fixture) -> None:  # type: ignore[no-untyped-def]
    """The payoff of comparing *effective* configurations: adding an override for code
    re-runs the code files and leaves the Markdown indexed."""
    await fixture.ingest(("handbook.md", MARKDOWN.encode()), ("util.py", PYTHON.encode()))

    view = await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"overrides": {"code": {"strategy": "code"}}}),
    )

    assert view.reindex_required
    assert view.reindex_formats == frozenset({"code"})
    queued = await fixture.service.reindex_connector(
        fixture.actor, fixture.connector.id, formats=sorted(view.reindex_formats)
    )
    assert queued == 1
    assert (await fixture.document("handbook.md")).status == "indexed"
    assert (await fixture.document("util.py")).status == "pending"


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


async def test_every_chunk_records_how_it_was_cut(fixture) -> None:  # type: ignore[no-untyped-def]
    """The same argument SPEC §9.4 makes for the embedding model, and a stronger one here:
    a connector reindexed halfway holds two chunkings, and without this nothing says which
    chunk is which."""
    await fixture.ingest(("handbook.md", MARKDOWN.encode()))
    document = await fixture.document("handbook.md")

    found = await fixture.service.document_chunks(fixture.actor, document.id)

    assert {chunk.payload["chunk_strategy"] for chunk in found.chunks} == {"recursive"}
    assert all(chunk.payload["chunk_fingerprint"] for chunk in found.chunks)
    assert document.chunk_fingerprint == found.chunks[0].payload["chunk_fingerprint"]


async def test_the_fingerprint_moves_when_the_chunking_does(fixture) -> None:  # type: ignore[no-untyped-def]
    await fixture.ingest(("handbook.md", MARKDOWN.encode()))
    before = (await fixture.document("handbook.md")).chunk_fingerprint

    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"chunk_size": 300, "overlap": 0}),
    )
    await fixture.service.reindex_connector(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    assert (await fixture.document("handbook.md")).chunk_fingerprint != before


async def test_a_windowed_chunk_carries_the_sentence_that_was_embedded(fixture) -> None:  # type: ignore[no-untyped-def]
    """Without it the first debugging session under ``sentence_window`` is "why does this
    chunk not contain the words I searched for", and the answer is not on the screen."""
    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(
            chunking={"strategy": "sentence_window", "window_sentences": 1, "overlap": 0}
        ),
    )
    await fixture.ingest(("handbook.md", MARKDOWN.encode()))
    document = await fixture.document("handbook.md")

    found = await fixture.service.document_chunks(fixture.actor, document.id)

    sample = found.chunks[1]
    assert sample.payload["embedded_text"] in sample.text
    assert sample.payload["embedded_text"] != sample.text
    assert sample.payload["window_sentences"] == 1


async def test_an_ordinary_chunk_carries_no_window_fields(fixture) -> None:  # type: ignore[no-untyped-def]
    """Payload size is multiplied by the corpus, and a key that is always equal to another
    key is a key somebody will one day read instead of the right one."""
    await fixture.ingest(("handbook.md", MARKDOWN.encode()))
    document = await fixture.document("handbook.md")

    found = await fixture.service.document_chunks(fixture.actor, document.id)

    assert "embedded_text" not in found.chunks[0].payload
    assert "window_sentences" not in found.chunks[0].payload


# ---------------------------------------------------------------------------
# the new failure mode
# ---------------------------------------------------------------------------


async def test_a_provider_outage_during_chunking_names_the_provider() -> None:
    """Not a generic chunking error. One of those sends somebody to read the splitter,
    where there is nothing wrong: under ``semantic`` this step embeds every sentence, and
    it is the embedding call that broke."""
    fixture = build_connectors(make_organization(), embedder=Refusing(retryable=False))
    await semantic(fixture)

    await fixture.ingest(("handbook.md", MARKDOWN.encode()))

    document = await fixture.document("handbook.md")
    assert document.status == "failed"
    assert document.reason == "chunking_embedding"
    assert "embedding provider" in (document.error or "")
    assert "semantic" in (document.error or "")


async def test_a_retryable_outage_raises_instead_of_failing_the_document() -> None:
    """The world is bad, not the document: this file will chunk perfectly in thirty
    seconds. Marking it ``failed`` would be a lie a customer has to notice and undo."""
    fixture = build_connectors(make_organization(), embedder=Refusing(retryable=True))
    await semantic(fixture)
    await fixture.upload(("handbook.md", MARKDOWN.encode()))

    with pytest.raises(EmbeddingError):
        await fixture.pipeline.ingest(
            organization_id=fixture.organization_id,
            document_id=(await fixture.document("handbook.md")).id,
        )

    assert (await fixture.document("handbook.md")).status in ("pending", "chunking")


async def test_a_retry_succeeds_once_the_provider_recovers() -> None:
    embedder = Refusing(retryable=False, failures=1)
    fixture = build_connectors(make_organization(), embedder=embedder)
    await semantic(fixture)
    await fixture.ingest(("handbook.md", MARKDOWN.encode()))
    assert (await fixture.document("handbook.md")).status == "failed"

    failed = await fixture.document("handbook.md")
    await fixture.service.reindex_document(fixture.actor, failed.id)
    await fixture.run_jobs()

    document = await fixture.document("handbook.md")
    assert document.status == "indexed"
    assert document.chunk_strategy == "semantic"
    assert document.error is None


# ---------------------------------------------------------------------------
# what it costs, and what says so
# ---------------------------------------------------------------------------


async def test_chunking_is_measured_by_strategy() -> None:
    """A duration averaged across strategies hides a millisecond of ``recursive`` inside
    two seconds of ``semantic``, which is the whole thing worth seeing."""
    registry = CollectorRegistry()
    metrics = build_chunking_metrics(registry)
    fixture = build_connectors(make_organization(), chunking_metrics=metrics)

    await fixture.ingest(("handbook.md", MARKDOWN.encode()))

    assert (
        registry.get_sample_value("chunking_duration_seconds_count", {"strategy": "recursive"}) == 1
    )
    assert (
        registry.get_sample_value("chunk_size_tokens_count", {"strategy": "recursive"}) or 0
    ) > 0
    assert registry.get_sample_value("chunk_size_tokens_count", {"strategy": "semantic"}) is None


async def test_semantic_embeds_far_more_than_it_indexes() -> None:
    """The cost that has to be shown before somebody switches a connector to it: one call
    for the sentences and one for the chunks, and there are several sentences per chunk."""
    counting = Refusing(retryable=False, failures=0)
    fixture = build_connectors(make_organization(), embedder=counting)
    await semantic(fixture)

    await fixture.ingest(("handbook.md", MARKDOWN.encode()))

    assert (await fixture.document("handbook.md")).status == "indexed"
    assert counting.calls == 2, "the sentence pass and the chunk pass, each batched"
