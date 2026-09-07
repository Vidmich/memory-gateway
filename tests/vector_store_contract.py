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
