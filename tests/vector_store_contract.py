"""One set of assertions, run against the memory store and against Qdrant.

The task file singles this contract out: *"Qdrant integration against a real container
(not a mock) — payload filters and delete-by-filter are exactly where a mock would lie."*
That is the reason the checks below are shaped the way they are. Nothing here asserts
that a particular float came back; every one asserts a *rule* about what a filter selects
and what a delete removes, because those are the two places where a hand-written double
agrees with itself and disagrees with the real thing.

Vectors are deliberately trivial — one-hot axes — so a similarity assertion is exact and
readable rather than approximately true.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Awaitable, Callable, Sequence

from app.services.vector_store import ChunkPoint, VectorStore, point_id

DIMENSION = 8

Check = Callable[[VectorStore, uuid.UUID], Awaitable[None]]
CHECKS: dict[str, Check] = {}


def check(function: Check) -> Check:
    CHECKS[function.__name__] = function
    return function


def axis(index: int) -> list[float]:
    """A one-hot vector. Cosine similarity to itself is 1 and to any other axis is 0, so
    every ranking assertion below is exact."""
    return [1.0 if position == index else 0.0 for position in range(DIMENSION)]


def at_similarity(score: float, *, spread: int) -> list[float]:
    """A unit vector whose cosine similarity to ``axis(0)`` is exactly ``score``.

    Weight ``score`` on axis 0 and the rest on axis ``spread``, which keeps the vector
    normalised and makes every score in a fixture something the test states rather than
    something it discovers. The one-hot vectors above can only express 1 and 0, and the
    checks that separate a real push-down from a filter applied afterwards need the
    scores of two groups to interleave.
    """
    vector = [0.0] * DIMENSION
    vector[0] = score
    vector[spread] = math.sqrt(max(0.0, 1.0 - score * score))
    return vector


def make_point(
    *,
    document_id: uuid.UUID,
    connector_id: uuid.UUID,
    organization_id: uuid.UUID,
    index: int = 0,
    vector_index: int = 0,
    text: str = "chunk text",
    section: str | None = None,
) -> ChunkPoint:
    return ChunkPoint(
        id=point_id(document_id, index),
        vector=axis(vector_index),
        payload={
            "org_id": str(organization_id),
            "connector_id": str(connector_id),
            "document_id": str(document_id),
            "source_name": "handbook.md",
            "source_uri": f"orgs/x/connectors/{connector_id}/handbook.md",
            "page_or_section": section,
            "chunk_index": index,
            "ingested_at": "2026-09-07T00:00:00+00:00",
            "content_hash": "abc",
            "text": text,
        },
    )


async def seed(
    store: VectorStore,
    organization_id: uuid.UUID,
    points: Sequence[ChunkPoint],
) -> None:
    await store.ensure_collection(organization_id, dimension=DIMENSION)
    await store.upsert(organization_id, points)


@check
async def a_chunk_is_found_by_its_own_vector(store: VectorStore, org: uuid.UUID) -> None:
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [make_point(document_id=document, connector_id=connector, organization_id=org)],
    )

    [match] = await store.search(org, axis(0))

    assert match.text == "chunk text"
    assert match.score > 0.99


@check
async def the_payload_carries_every_spec_field(store: VectorStore, org: uuid.UUID) -> None:
    """SPEC §9.3's chunk metadata. Task 10 reads all of it to build a citation, and a
    field silently dropped by the store would only show up there."""
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                section="Guide > Setup",
            )
        ],
    )

    [match] = await store.search(org, axis(0))

    assert match.payload["org_id"] == str(org)
    assert match.payload["connector_id"] == str(connector)
    assert match.payload["document_id"] == str(document)
    assert match.payload["source_name"] == "handbook.md"
    assert match.payload["page_or_section"] == "Guide > Setup"
    assert match.payload["chunk_index"] == 0
    assert match.payload["content_hash"] == "abc"


@check
async def results_come_back_best_first(store: VectorStore, org: uuid.UUID) -> None:
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                index=0,
                vector_index=1,
                text="far",
            ),
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                index=1,
                vector_index=0,
                text="near",
            ),
        ],
    )

    matches = await store.search(org, axis(0), limit=5)

    assert matches[0].text == "near"


@check
async def the_limit_is_honoured(store: VectorStore, org: uuid.UUID) -> None:
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=document, connector_id=connector, organization_id=org, index=index
            )
            for index in range(5)
        ],
    )

    assert len(await store.search(org, axis(0), limit=2)) == 2


@check
async def a_connector_filter_excludes_other_connectors(store: VectorStore, org: uuid.UUID) -> None:
    """The payload filter the task file names. A double that ignored it would pass every
    other check here, and task 10's gateway would retrieve from connectors it was never
    pointed at."""
    wanted, other = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=uuid.uuid4(),
                connector_id=wanted,
                organization_id=org,
                text="wanted",
            ),
            make_point(
                document_id=uuid.uuid4(),
                connector_id=other,
                organization_id=org,
                text="other",
            ),
        ],
    )

    matches = await store.search(org, axis(0), connector_ids=[wanted])

    assert [match.text for match in matches] == ["wanted"]


@check
async def a_score_threshold_drops_weak_matches(store: VectorStore, org: uuid.UUID) -> None:
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                vector_index=1,
            )
        ],
    )

    assert await store.search(org, axis(0), min_score=0.5) == []


@check
async def re_upserting_the_same_chunk_replaces_it(store: VectorStore, org: uuid.UUID) -> None:
    """Deterministic ids, doing their job. Without them a re-ingestion doubles the index
    and every query returns the same chunk twice."""
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [make_point(document_id=document, connector_id=connector, organization_id=org, text="v1")],
    )
    await seed(
        store,
        org,
        [make_point(document_id=document, connector_id=connector, organization_id=org, text="v2")],
    )

    matches = await store.search(org, axis(0))

    assert [match.text for match in matches] == ["v2"]
    assert await store.count(org, document_id=document) == 1


@check
async def deleting_a_document_removes_every_chunk_of_it(store: VectorStore, org: uuid.UUID) -> None:
    """Delete-by-filter, not by the ids the caller thinks it has. A document that shrank
    from forty chunks to thirty has ten whose ids nobody remembers."""
    document, other, connector = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=document, connector_id=connector, organization_id=org, index=index
            )
            for index in range(4)
        ]
        + [make_point(document_id=other, connector_id=connector, organization_id=org, text="keep")],
    )

    await store.delete_document(org, document)

    assert await store.count(org, document_id=document) == 0
    assert await store.count(org, document_id=other) == 1


@check
async def deleting_a_connector_removes_only_its_chunks(store: VectorStore, org: uuid.UUID) -> None:
    doomed, kept = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(document_id=uuid.uuid4(), connector_id=doomed, organization_id=org),
            make_point(document_id=uuid.uuid4(), connector_id=kept, organization_id=org),
        ],
    )

    await store.delete_connector(org, doomed)

    assert await store.count(org, connector_id=doomed) == 0
    assert await store.count(org, connector_id=kept) == 1


@check
async def deleting_from_a_collection_that_does_not_exist_is_fine(
    store: VectorStore, org: uuid.UUID
) -> None:
    """A delete asks for an end state, and for a tenant that never indexed anything that
    end state already holds. Raising would fail every connector deletion made before the
    first upload."""
    await store.delete_document(org, uuid.uuid4())
    await store.delete_connector(org, uuid.uuid4())


@check
async def searching_a_collection_that_does_not_exist_returns_nothing(
    store: VectorStore, org: uuid.UUID
) -> None:
    assert await store.search(org, axis(0)) == []
    assert await store.count(org) == 0


@check
async def a_collection_reports_the_width_it_was_built_with(
    store: VectorStore, org: uuid.UUID
) -> None:
    """Retrieval asks before it searches, so an index built by a different embedding
    model is refused loudly instead of returning neighbours that mean nothing."""
    await store.ensure_collection(org, dimension=DIMENSION)

    assert await store.dimension(org) == DIMENSION


@check
async def an_organization_with_no_collection_has_no_width(
    store: VectorStore, org: uuid.UUID
) -> None:
    """``None``, not zero: nothing has been indexed, which is not a mismatch."""
    assert await store.dimension(org) is None


@check
async def a_dropped_collection_stops_reporting_a_width(store: VectorStore, org: uuid.UUID) -> None:
    """The one case a cached width could be wrong about, so it is the one asserted."""
    await store.ensure_collection(org, dimension=DIMENSION)
    assert await store.dimension(org) == DIMENSION

    await store.drop(org)

    assert await store.dimension(org) is None


@check
async def a_documents_chunks_come_back_in_the_order_they_were_cut(
    store: VectorStore, org: uuid.UUID
) -> None:
    """The chunk inspector. Ordered by ``chunk_index`` rather than by point id, because
    the ids are hashes and a store's natural order is therefore arbitrary."""
    await store.ensure_collection(org, dimension=DIMENSION)
    document, connector = uuid.uuid4(), uuid.uuid4()
    await store.upsert(
        org,
        [
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                index=index,
                text=f"chunk {index}",
            )
            # Inserted out of order on purpose: an implementation that returned insertion
            # order would pass a sorted set-up and fail on a real one.
            for index in (2, 0, 1)
        ],
    )

    found = await store.chunks(org, document)

    assert [chunk.index for chunk in found] == [0, 1, 2]
    assert [chunk.text for chunk in found] == ["chunk 0", "chunk 1", "chunk 2"]


@check
async def chunks_are_scoped_to_one_document(store: VectorStore, org: uuid.UUID) -> None:
    """The inspector is opened from a row, and it must answer about that row."""
    connector = uuid.uuid4()
    wanted, other = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=wanted, connector_id=connector, organization_id=org, text="mine"
            ),
            make_point(
                document_id=other, connector_id=connector, organization_id=org, text="theirs"
            ),
        ],
    )

    assert [chunk.text for chunk in await store.chunks(org, wanted)] == ["mine"]


@check
async def a_document_with_nothing_indexed_has_no_chunks(store: VectorStore, org: uuid.UUID) -> None:
    """A document that reports ``indexed`` and returns nothing here is the case the
    inspector exists to make visible, so the empty answer has to be an answer."""
    await store.ensure_collection(org, dimension=DIMENSION)

    assert await store.chunks(org, uuid.uuid4()) == []


@check
async def dropping_a_collection_removes_everything(store: VectorStore, org: uuid.UUID) -> None:
    await seed(
        store,
        org,
        [make_point(document_id=uuid.uuid4(), connector_id=uuid.uuid4(), organization_id=org)],
    )

    await store.drop(org)

    assert await store.search(org, axis(0)) == []


@check
async def ensuring_a_collection_twice_is_harmless(store: VectorStore, org: uuid.UUID) -> None:
    """Called on every ingestion rather than once at provisioning, so a collection
    somebody deleted by hand heals on the next upload instead of needing an operator."""
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [make_point(document_id=document, connector_id=connector, organization_id=org)],
    )

    await store.ensure_collection(org, dimension=DIMENSION)

    assert await store.count(org) == 1


@check
async def one_organization_never_sees_another(store: VectorStore, org: uuid.UUID) -> None:
    """SPEC §5.3 for the index. The collection name carries the tenant id, so this is
    structural rather than a filter somebody has to remember."""
    other = uuid.uuid4()
    connector = uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=uuid.uuid4(),
                connector_id=connector,
                organization_id=org,
                text="ours",
            )
        ],
    )
    await seed(
        store,
        other,
        [
            make_point(
                document_id=uuid.uuid4(),
                connector_id=connector,
                organization_id=other,
                text="theirs",
            )
        ],
    )

    try:
        # The *same* connector id, on purpose: even a caller that got the filter right
        # and the tenant wrong must come back with its own row.
        matches = await store.search(org, axis(0), connector_ids=[connector])

        assert [match.text for match in matches] == ["ours"]
    finally:
        await store.drop(other)


@check
async def upserting_nothing_is_not_an_error(store: VectorStore, org: uuid.UUID) -> None:
    """A document that extracted to no text still reaches the indexing step."""
    await store.ensure_collection(org, dimension=DIMENSION)
    await store.upsert(org, [])


__all__ = ["CHECKS", "DIMENSION", "axis", "make_point", "seed"]


@check
async def the_score_is_a_similarity_and_not_a_distance(store: VectorStore, org: uuid.UUID) -> None:
    """The conversion every backend that speaks in distances has to get right.

    It is worth its own check because getting it backwards does not raise: the results
    come back ranked confidently in exactly the wrong order, and the only symptom is that
    retrieved context stops being relevant. Cosine *distance* would report 0 for the
    identical chunk and 1 for the orthogonal one — both assertions below invert.
    """
    document, connector = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        org,
        [
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                index=0,
                vector_index=0,
                text="same",
            ),
            make_point(
                document_id=document,
                connector_id=connector,
                organization_id=org,
                index=1,
                vector_index=1,
                text="orthogonal",
            ),
        ],
    )

    matches = {match.text: match.score for match in await store.search(org, axis(0), limit=5)}

    assert matches["same"] > 0.99
    assert abs(matches["orthogonal"]) < 0.01


@check
async def the_limit_counts_results_after_the_connector_filter(
    store: VectorStore, org: uuid.UUID
) -> None:
    """``limit`` means "this many chunks the caller asked for", not "this many candidates,
    some of which are then discarded".

    The scores interleave deliberately. A backend that takes the global top ``limit`` and
    filters afterwards sees ``other`` at 0.95 and 0.85 occupying two of its three slots
    and returns one result; a backend that pushes the filter down returns three. Both
    implementations pass every other check in this file.
    """
    wanted, other = uuid.uuid4(), uuid.uuid4()
    points = [
        ChunkPoint(
            id=point_id(uuid.uuid4(), 0),
            vector=at_similarity(score, spread=spread),
            payload={"connector_id": str(connector), "document_id": str(uuid.uuid4()), "text": tag},
        )
        for spread, (connector, score, tag) in enumerate(
            [
                (other, 0.95, "other"),
                (wanted, 0.90, "wanted"),
                (other, 0.85, "other"),
                (wanted, 0.80, "wanted"),
                (other, 0.75, "other"),
                (wanted, 0.70, "wanted"),
                (other, 0.65, "other"),
            ],
            start=1,
        )
    ]
    await seed(store, org, points)

    matches = await store.search(org, axis(0), connector_ids=[wanted], limit=3)

    assert [match.text for match in matches] == ["wanted", "wanted", "wanted"]


@check
async def the_floor_and_the_filter_apply_together(store: VectorStore, org: uuid.UUID) -> None:
    """Both narrowings at once, which is what task 10 actually issues on every request.

    ``doc_min_score`` and the gateway's connector list arrive together, and a backend that
    honours either one alone still answers plausibly — with another connector's chunks, or
    with chunks nobody would call relevant.
    """
    wanted, other = uuid.uuid4(), uuid.uuid4()
    points = [
        ChunkPoint(
            id=point_id(uuid.uuid4(), 0),
            vector=at_similarity(score, spread=spread),
            payload={"connector_id": str(connector), "document_id": str(uuid.uuid4()), "text": tag},
        )
        for spread, (connector, score, tag) in enumerate(
            [
                (other, 0.95, "other strong"),
                (wanted, 0.90, "wanted strong"),
                (wanted, 0.20, "wanted weak"),
                (other, 0.10, "other weak"),
            ],
            start=1,
        )
    ]
    await seed(store, org, points)

    matches = await store.search(org, axis(0), connector_ids=[wanted], min_score=0.5, limit=10)

    assert [match.text for match in matches] == ["wanted strong"]
