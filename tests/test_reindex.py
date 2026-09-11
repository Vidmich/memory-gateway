"""Rebuilding every collection under a new embedding model, without a gap in retrieval.

The acceptance criterion this file exists for is the hardest one in task 17 to state
convincingly: *"a search loop running throughout returns valid results at every moment."*
A test that searched before and after would pass against a delete-then-rebuild, which is
exactly the design the alias exists to avoid — so
:func:`test_a_search_running_throughout_never_sees_an_empty_index` searches *between every
step of the run*, driven from inside the copy rather than from a second task, because a
racing task would make the assertion depend on the scheduler.

The rest is the procedure's other promises: resumption without duplicates, a dimension
change that actually changes the width, verification that fails closed, and one run at a
time per scope.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import Conflict
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import Connector, Document, Organization
from app.schemas.platform import EmbeddingChoice
from app.services.embeddings import Embedder
from app.services.reindex import estimated_cost_lines, progress_of
from app.services.reindex_store import EMBEDDING, FAILED, SUCCEEDED, SWAPPED
from app.services.vector_index import successor, version_of, versioned
from app.services.vector_store import ChunkPoint, collection_for, point_id
from tests.platform_support import DIMENSION, PlatformFixture, build_platform


@pytest.fixture
def platform() -> PlatformFixture:
    return build_platform()


@pytest.fixture
def actor() -> Actor:
    user = uuid7()
    return Actor(
        user_id=user,
        scope=TenantScope(role="superadmin", organization_id=None),
        label="ops@example.com",
    )


async def seed(platform: PlatformFixture, *, chunks: int = 12) -> uuid.UUID:
    """One organization with an indexed document."""
    organization = uuid7()
    platform.db.organizations[organization] = _organization(organization)
    await platform.index_chunks(
        organization,
        uuid7(),
        *[f"chunk number {index} about invoices and refunds" for index in range(chunks)],
    )
    return organization


def _organization(identifier: uuid.UUID) -> Organization:
    return Organization(
        id=identifier, name="Acme", slug=f"acme-{identifier.hex[:6]}", status="active"
    )


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------


def test_a_fresh_tenant_is_created_behind_an_alias() -> None:
    organization = uuid7()

    assert versioned(organization, 1) == f"{collection_for(organization)}_v1"


def test_an_unversioned_collection_is_version_zero_so_its_successor_is_one() -> None:
    """A tenant indexed before task 17 has a collection named like the alias. Calling that
    version zero is what makes ``successor`` need no branch for it."""
    organization = uuid7()

    assert version_of(collection_for(organization)) == 0
    assert successor(organization, collection_for(organization)) == versioned(organization, 1)
    assert successor(organization, versioned(organization, 3)) == versioned(organization, 4)


async def test_ensure_collection_creates_the_alias_not_a_bare_collection(
    platform: PlatformFixture,
) -> None:
    organization = uuid7()

    await platform.vectors.ensure_collection(organization, dimension=DIMENSION)

    assert platform.vectors.live(organization) == versioned(organization, 1)


# ---------------------------------------------------------------------------
# estimating
# ---------------------------------------------------------------------------


async def test_the_estimate_counts_what_the_run_will_copy(platform: PlatformFixture) -> None:
    """Counted rather than guessed, because the chunks are in the index with their text.

    That matters for a number somebody agrees to before spending it: an estimate derived
    from a document count would be wrong by whatever the chunking produced.
    """
    organization = await seed(platform, chunks=7)

    estimate = await platform.reindexer.estimate()

    assert estimate.points == 7
    assert estimate.organizations == 1
    assert estimate.collections == [versioned(organization, 1)]
    assert estimate.tokens > 0


async def test_an_estimate_for_a_platform_with_nothing_indexed_is_zero(
    platform: PlatformFixture,
) -> None:
    estimate = await platform.reindexer.estimate()

    assert estimate.points == 0
    assert estimate.collections == []


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


async def test_a_run_copies_every_chunk_into_the_next_collection(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed(platform, chunks=9)

    run = await platform.reindexer.start(actor)
    finished = await platform.reindexer.run(run.id)

    assert finished.status == SUCCEEDED
    assert [target.status for target in finished.targets] == [SWAPPED]
    assert await platform.index.count_points(versioned(organization, 2)) == 9


async def test_the_alias_moves_and_searches_follow_it(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed(platform, chunks=5)

    run = await platform.reindexer.start(actor)
    await platform.reindexer.run(run.id)

    assert platform.vectors.live(organization) == versioned(organization, 2)
    vector = (await platform.embedder.embed(["invoices"]))[0]
    assert await platform.vectors.search(organization, vector, limit=3)


async def test_a_search_running_throughout_never_sees_an_empty_index(
    platform: PlatformFixture, actor: Actor
) -> None:
    """The acceptance criterion, asserted between every step rather than at the ends.

    The search goes through the ordinary port — the same call a request makes — so what is
    being proved is that a *client* is unaffected, not that some internal invariant held.
    """
    organization = await seed(platform, chunks=20)
    vector = (await platform.embedder.embed(["refunds"]))[0]
    seen: list[int] = []

    async def watched_upsert(collection: str, points: Sequence[ChunkPoint]) -> None:
        await original(collection, points)
        seen.append(len(await platform.vectors.search(organization, vector, limit=5)))

    original = platform.index.upsert_into
    platform.index.upsert_into = watched_upsert  # type: ignore[method-assign]

    run = await platform.reindexer.start(actor)
    await platform.reindexer.run(run.id)

    assert seen, "the copy never ran, so nothing was observed"
    assert all(count > 0 for count in seen), seen
    assert len(await platform.vectors.search(organization, vector, limit=5)) > 0


async def test_a_dimension_change_produces_a_collection_of_the_new_width(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed(platform, chunks=4)
    choice = EmbeddingChoice(provider="hash", name="wider-bow", dimension=128)

    run = await platform.reindexer.start(actor, choice=choice)
    finished = await platform.reindexer.run(run.id)

    assert finished.status == SUCCEEDED
    assert await platform.index.collection_dimension(versioned(organization, 2)) == 128
    assert await platform.vectors.dimension(organization) == 128


async def test_the_platform_embedding_is_written_only_when_the_run_succeeds(
    platform: PlatformFixture, actor: Actor
) -> None:
    """The whole reason the setting is not written up front.

    Between a PATCH and the swap, every new ingestion would embed with the new model and
    upsert into a collection of the old width — which Qdrant refuses. So serving keeps the
    old model until there is a collection that agrees with the new one.
    """
    await seed(platform, chunks=3)
    choice = EmbeddingChoice(provider="hash", name="wider-bow", dimension=128)
    run = await platform.reindexer.start(actor, choice=choice)

    assert (await platform.platform_settings.current()).embedding.name != "wider-bow"

    await platform.reindexer.run(run.id)

    assert (await platform.platform_settings.current()).embedding.name == "wider-bow"
    assert (await platform.platform_settings.current()).embedding.dimension == 128


async def test_a_killed_run_resumes_and_finishes_without_duplicates(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Killed halfway, restarted, and the result is the same set of points.

    Deterministic ids are what make that true: a repeated page overwrites what it already
    wrote, so resumption costs time rather than correctness. The assertion is on the
    *count*, because a duplicate would show up there and nowhere else.
    """
    organization = await seed(platform, chunks=30)
    run = await platform.reindexer.start(actor)

    pages = 0
    original = platform.index.upsert_into

    async def die_after_two(collection: str, points: Sequence[ChunkPoint]) -> None:
        nonlocal pages
        pages += 1
        if pages > 2:
            raise RuntimeError("the worker was killed")
        await original(collection, points)

    platform.index.upsert_into = die_after_two  # type: ignore[method-assign]
    platform.reindexer._batch = 8

    killed = await platform.reindexer.run(run.id)
    assert killed.status == FAILED

    partial = await platform.index.count_points(versioned(organization, 2))
    assert 0 < partial < 30, "the kill did not land mid-copy, so nothing is being resumed"

    platform.index.upsert_into = original  # type: ignore[method-assign]
    # A new run rather than a retry of the same one: a failed target stays failed, which
    # is the honest behaviour — the operator decides whether to try again. It targets the
    # *same* collection, because the alias never moved, and the partially-filled one is
    # reused rather than rebuilt.
    second = await platform.reindexer.start(actor)
    finished = await platform.reindexer.run(second.id)

    assert finished.status == SUCCEEDED
    assert finished.targets[0].collection == versioned(organization, 2)
    assert await platform.index.count_points(versioned(organization, 2)) == 30


async def test_a_failed_target_leaves_the_old_collection_serving(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed(platform, chunks=6)

    async def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("the provider is down")

    platform.index.upsert_into = refuse  # type: ignore[method-assign]
    run = await platform.reindexer.start(actor)

    finished = await platform.reindexer.run(run.id)

    assert finished.status == FAILED
    assert platform.vectors.live(organization) == versioned(organization, 1)
    vector = (await platform.embedder.embed(["invoices"]))[0]
    assert await platform.vectors.search(organization, vector, limit=3)


async def test_a_short_copy_is_caught_by_the_count_and_never_swaps(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Verification fails closed. A collection missing chunks is one whose answers are
    quietly worse, which is the failure mode that takes months to notice."""
    organization = await seed(platform, chunks=10)
    run = await platform.reindexer.start(actor)
    original = platform.index.upsert_into
    dropped = {"count": 0}

    async def drop_the_last(collection: str, points: Sequence[ChunkPoint]) -> None:
        dropped["count"] += 1
        if dropped["count"] > 1:
            return
        await original(collection, points)

    platform.index.upsert_into = drop_the_last  # type: ignore[method-assign]
    platform.reindexer._batch = 4

    finished = await platform.reindexer.run(run.id)

    assert finished.status == FAILED
    assert platform.vectors.live(organization) == versioned(organization, 1)


async def test_a_second_reindex_for_the_same_scope_is_refused(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Two runs would fight over one alias, and the operator's next question is always
    "what is already running" — which the message answers."""
    await seed(platform)
    first = await platform.reindexer.start(actor)

    with pytest.raises(Conflict) as refused:
        await platform.reindexer.start(actor)

    assert "already running" in str(refused.value)
    assert first.status == "running"


async def test_an_organization_scoped_run_blocks_a_platform_one(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Overlap rather than equality: a platform run would rebuild the very collection the
    narrower one is already rebuilding."""
    organization = await seed(platform)
    await platform.reindexer.start(actor, organization_id=organization)

    with pytest.raises(Conflict):
        await platform.reindexer.start(actor)


async def test_a_finished_run_does_not_block_the_next_one(
    platform: PlatformFixture, actor: Actor
) -> None:
    await seed(platform, chunks=2)
    first = await platform.reindexer.start(actor)
    await platform.reindexer.run(first.id)

    second = await platform.reindexer.start(actor)

    assert second.id != first.id


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


async def test_progress_offers_no_eta_before_anything_has_been_copied(
    platform: PlatformFixture, actor: Actor
) -> None:
    """A number extrapolated from nothing is worse than a blank on a screen somebody is
    using to decide whether to wait."""
    await seed(platform, chunks=10)
    run = await platform.reindexer.start(actor)

    assert progress_of(run).eta_seconds is None
    assert progress_of(run).total == 10


async def test_progress_is_complete_once_the_run_has_finished(
    platform: PlatformFixture, actor: Actor
) -> None:
    await seed(platform, chunks=10)
    run = await platform.reindexer.start(actor)
    finished = await platform.reindexer.run(run.id)

    progress = progress_of(finished)

    assert progress.done == 10
    assert progress.fraction == 1.0


async def test_a_target_records_the_collection_it_was_building(
    platform: PlatformFixture, actor: Actor
) -> None:
    """A run that dies between "created" and "swapped" leaves a collection behind, and an
    operator cleaning up should not have to guess its version."""
    organization = await seed(platform, chunks=2)

    run = await platform.reindexer.start(actor)

    assert run.targets[0].collection == versioned(organization, 2)
    assert run.targets[0].status not in (SWAPPED, EMBEDDING)


# ---------------------------------------------------------------------------
# the recut path (task 20)
# ---------------------------------------------------------------------------


class RecordingRecutter:
    """A recutter that rebuilds each document into a fixed number of points.

    A double rather than the real pipeline, deliberately: what is under test here is the
    *reindexer's* arithmetic and its decisions — which connectors are recut, what the count
    check compares against, what happens when one document will not read. That the real
    recut produces the right chunks is
    ``tests/test_chunking_pipeline.py``'s job, and running an extraction stack through this
    file would test both badly.
    """

    def __init__(self, *, per_document: int = 3, broken: set[uuid.UUID] | None = None) -> None:
        self.per_document = per_document
        self.broken = broken or set()
        self.seen: list[uuid.UUID] = []
        self.models: list[str] = []

    async def recut(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID, embedder: Embedder
    ) -> list[ChunkPoint]:
        self.seen.append(document_id)
        self.models.append(embedder.model)
        if document_id in self.broken:
            raise RuntimeError("this object is no longer in storage")
        vectors = await embedder.embed([f"recut {index}" for index in range(self.per_document)])
        return [
            ChunkPoint(
                id=point_id(document_id, index),
                vector=list(vector),
                payload={
                    "text": f"recut {index}",
                    "document_id": str(document_id),
                    "chunk_index": index,
                },
            )
            for index, vector in enumerate(vectors)
        ]


def add_connector(
    platform: PlatformFixture,
    organization: uuid.UUID,
    *,
    strategy: str,
    documents: int = 1,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """A connector row and its indexed documents, without running ingestion."""
    connector = Connector(
        id=uuid7(),
        organization_id=organization,
        name=f"{strategy} source",
        type="managed_file_drop",
        chunking={"strategy": strategy},
        status="ready",
    )
    platform.db.connectors[connector.id] = connector
    ids = []
    for index in range(documents):
        document = Document(
            id=uuid7(),
            organization_id=organization,
            connector_id=connector.id,
            source_uri=f"orgs/{organization}/{index}.md",
            source_name=f"{index}.md",
            mime_type="text/markdown",
            size_bytes=64,
            status="indexed",
            chunk_count=2,
        )
        platform.db.documents[document.id] = document
        ids.append(document.id)
    return connector.id, ids


async def test_the_estimate_names_the_connectors_that_have_to_be_recut() -> None:
    """A different *kind* of cost, not a bigger one: object reads, extraction and
    re-chunking, none of which the token figure covers. An operator deciding whether to
    change the platform model needs it before they decide, not from the run's duration."""
    platform = build_platform(recutter=RecordingRecutter())
    organization = await seed(platform, chunks=4)
    add_connector(platform, organization, strategy="semantic", documents=3)
    add_connector(platform, organization, strategy="recursive", documents=9)

    estimate = await platform.reindexer.estimate(
        choice=EmbeddingChoice(provider="hash", name="hash-wide", dimension=DIMENSION)
    )

    assert estimate.recut_connectors == 1
    assert estimate.recut_documents == 3
    assert any("recut from object storage" in line for line in estimated_cost_lines(estimate))


async def test_nothing_is_recut_when_no_connector_chunks_with_the_model() -> None:
    platform = build_platform(recutter=RecordingRecutter())
    organization = await seed(platform, chunks=4)
    add_connector(platform, organization, strategy="recursive", documents=5)

    estimate = await platform.reindexer.estimate()

    assert estimate.recut_connectors == 0
    assert estimate.recut_documents == 0


async def test_a_semantic_connector_is_recut_and_the_rest_is_re_embedded(
    actor: Actor,
) -> None:
    """The expensive case, end to end. The copied points keep their text; the recut ones
    are rebuilt from storage with the *new* model deciding the boundaries."""
    recutter = RecordingRecutter(per_document=3)
    platform = build_platform(recutter=recutter)
    organization = await seed(platform, chunks=0)
    semantic_connector, semantic_documents = add_connector(
        platform, organization, strategy="semantic", documents=2
    )
    plain_connector, _ = add_connector(platform, organization, strategy="recursive")
    await platform.index_chunks(
        organization, uuid7(), "old boundaries here", connector_id=semantic_connector
    )
    await platform.index_chunks(organization, uuid7(), "a", "b", "c", connector_id=plain_connector)

    run = await platform.reindexer.start(actor)
    finished = await platform.reindexer.run(run.id)

    assert finished.status == SUCCEEDED
    assert sorted(recutter.seen) == sorted(semantic_documents)
    live = await platform.index.live_collection(organization)
    assert live is not None
    texts = {
        str(point.payload.get("text"))
        for point in (await platform.index.scroll(live, cursor=None, limit=100)).points
    }
    assert texts == {"a", "b", "c", "recut 0", "recut 1", "recut 2"}
    assert "old boundaries here" not in texts, "the semantic connector's old cut is gone"


async def test_the_recut_uses_the_model_the_run_is_moving_to(actor: Actor) -> None:
    """Recutting with the serving model would make the whole extra expense buy nothing:
    the boundaries would still be the old model's opinion."""
    recutter = RecordingRecutter()
    platform = build_platform(recutter=recutter)
    organization = await seed(platform, chunks=2)
    add_connector(platform, organization, strategy="semantic")

    run = await platform.reindexer.start(
        actor, choice=EmbeddingChoice(provider="hash", name="hash-next", dimension=DIMENSION)
    )
    await platform.reindexer.run(run.id)

    assert recutter.models == ["hash-next"]


async def test_a_document_that_cannot_be_recut_does_not_fail_the_tenant(
    actor: Actor,
) -> None:
    """One file of a corpus. The old collection still has whatever it had for it, and
    failing a whole tenant's reindex because somebody deleted an object would be a far
    worse trade — so the count check compares against what was actually produced."""
    recutter = RecordingRecutter()
    platform = build_platform(recutter=recutter)
    organization = await seed(platform, chunks=2)
    _, documents = add_connector(platform, organization, strategy="semantic", documents=2)
    recutter.broken = {documents[0]}

    run = await platform.reindexer.start(actor)
    finished = await platform.reindexer.run(run.id)

    assert finished.status == SUCCEEDED
    assert finished.targets[0].status == SWAPPED


async def test_a_reindexer_with_no_recutter_refuses_rather_than_copying(actor: Actor) -> None:
    """A silent fallback would re-embed a semantic connector's old boundaries, report
    success, and leave a collection whose cut came from a model no longer in use — with
    nothing anywhere saying so."""
    platform = build_platform()  # no recutter, which is how the API process is built
    organization = await seed(platform, chunks=2)
    add_connector(platform, organization, strategy="semantic")

    run = await platform.reindexer.start(actor)
    finished = await platform.reindexer.run(run.id)

    assert finished.status == FAILED
    assert "recut" in (finished.error or "")
    assert await platform.index.live_collection(organization) == versioned(organization, 1)


# ---------------------------------------------------------------------------
# the runs a recut leaves behind (task 104)
# ---------------------------------------------------------------------------


class RecordingTracker:
    """The port the reindexer reports its recuts to, recording every call."""

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.settled: list[dict[str, Any]] = []
        self.adopted: list[tuple[uuid.UUID, str]] = []

    async def open_for_reindex(self, **kwargs: Any) -> Any:
        self.opened.append(kwargs)
        return SimpleNamespace(id=uuid7())

    async def settle_recut(self, **kwargs: Any) -> None:
        self.settled.append(kwargs)

    async def adopt_embedding_model(self, organization_id: uuid.UUID, embedding_model: str) -> None:
        self.adopted.append((organization_id, embedding_model))


async def test_a_platform_reindex_opens_a_run_per_recut_connector_and_adopts_at_the_swap(
    actor: Actor,
) -> None:
    """The connector's own screen shows the platform operation's progress for its
    documents: one run per recut connector, trigger ``embedding_model``, every document
    settled — the broken one as failed — and at the swap every tenant's rows re-marked
    under the model the platform now uses."""
    recutter = RecordingRecutter(per_document=3)
    tracker = RecordingTracker()
    platform = build_platform(recutter=recutter, tracker=tracker)
    organization = await seed(platform, chunks=2)
    semantic, documents = add_connector(platform, organization, strategy="semantic", documents=2)
    add_connector(platform, organization, strategy="recursive")
    recutter.broken = {documents[1]}

    run = await platform.reindexer.start(
        actor, choice=EmbeddingChoice(provider="hash", name="hash-next", dimension=DIMENSION)
    )
    finished = await platform.reindexer.run(run.id)

    assert finished.status == SUCCEEDED
    [opened] = tracker.opened
    assert opened["connector_id"] == semantic and opened["reindex_run_id"] == run.id
    assert sorted(opened["documents"]) == sorted(documents)
    outcomes = {entry["document_id"]: entry["outcome"] for entry in tracker.settled}
    assert outcomes == {documents[0]: "done", documents[1]: "failed"}
    done = next(entry for entry in tracker.settled if entry["outcome"] == "done")
    assert done["chunk_count"] == 3
    assert tracker.adopted == [(organization, "hash-next")]


async def test_a_copied_point_carries_the_model_it_was_re_embedded_with(actor: Actor) -> None:
    """The copy changes exactly one segment of a point's index fingerprint; a point
    without one is copied as it was."""
    from app.services.index_fingerprint import with_embedding_model

    platform = build_platform()
    organization = await seed(platform, chunks=0)
    document = uuid7()
    recorded = "ch=abc;em=old;tk=t;sm=-;xv=1"
    await platform.vectors.ensure_collection(organization, dimension=DIMENSION)
    vectors = await platform.embedder.embed(["with", "without"])
    await platform.vectors.upsert(
        organization,
        [
            ChunkPoint(
                id=point_id(document, 0),
                vector=list(vectors[0]),
                payload={
                    "text": "with",
                    "document_id": str(document),
                    "chunk_index": 0,
                    "index_fingerprint": recorded,
                },
            ),
            ChunkPoint(
                id=point_id(document, 1),
                vector=list(vectors[1]),
                payload={"text": "without", "document_id": str(document), "chunk_index": 1},
            ),
        ],
    )

    run = await platform.reindexer.start(
        actor, choice=EmbeddingChoice(provider="hash", name="hash-next", dimension=DIMENSION)
    )
    await platform.reindexer.run(run.id)

    live = await platform.index.live_collection(organization)
    assert live is not None
    by_text = {
        str(point.payload.get("text")): point.payload
        for point in (await platform.index.scroll(live, cursor=None, limit=10)).points
    }
    assert by_text["with"]["index_fingerprint"] == with_embedding_model(recorded, "hash-next")
    assert "index_fingerprint" not in by_text["without"]
