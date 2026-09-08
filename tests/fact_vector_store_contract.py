"""One set of assertions for the conversation-memory index, memory and Qdrant alike.

The same reasoning as ``tests/vector_store_contract.py``, aimed at a different filter.
Every check here is about ``end_user_id``: what a search selects, and what a purge
removes. Those are the two operations where a hand-written double agrees with itself and
disagrees with a server — and here the consequence of the disagreement is one person's
durable facts reaching another person's prompt, which is the worst outcome this system
has.

Vectors are one-hot axes, so every similarity assertion is exact rather than approximate.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from app.services.fact_vectors import FactPoint, FactVectorStore, fact_payload

DIMENSION = 8

Check = Callable[[FactVectorStore, uuid.UUID], Awaitable[None]]
CHECKS: dict[str, Check] = {}


def check(function: Check) -> Check:
    CHECKS[function.__name__] = function
    return function


def axis(index: int) -> list[float]:
    return [1.0 if position == index else 0.0 for position in range(DIMENSION)]


def make_point(
    *,
    organization_id: uuid.UUID,
    end_user_id: uuid.UUID,
    fact_id: uuid.UUID | None = None,
    vector_index: int = 0,
    kind: str = "preference",
    confidence: float = 1.0,
) -> FactPoint:
    identifier = fact_id or uuid.uuid4()
    return FactPoint(
        id=str(identifier),
        vector=axis(vector_index),
        payload=fact_payload(
            organization_id=organization_id,
            end_user_id=end_user_id,
            kind=kind,
            confidence=confidence,
            created_at=1_757_000_000.0,
        ),
    )


async def seed(store: FactVectorStore, organization_id: uuid.UUID, *points: FactPoint) -> None:
    await store.ensure_collection(organization_id, dimension=DIMENSION)
    await store.upsert(organization_id, list(points))


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


@check
async def a_search_returns_the_matching_fact(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    alice = uuid.uuid4()
    point = make_point(organization_id=organization_id, end_user_id=alice, vector_index=0)
    await seed(store, organization_id, point)

    found = await store.search(organization_id, axis(0), end_user_id=alice)

    assert [match.id for match in found] == [point.id]
    assert found[0].score > 0.99


@check
async def a_search_never_crosses_to_another_end_user(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """The rule the whole feature rests on. Both facts are identical vectors, so nothing
    but the filter can keep them apart."""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    hers = make_point(organization_id=organization_id, end_user_id=alice)
    his = make_point(organization_id=organization_id, end_user_id=bob)
    await seed(store, organization_id, hers, his)

    found = await store.search(organization_id, axis(0), end_user_id=alice)

    assert [match.id for match in found] == [hers.id]


@check
async def a_score_floor_is_applied(store: FactVectorStore, organization_id: uuid.UUID) -> None:
    alice = uuid.uuid4()
    near = make_point(organization_id=organization_id, end_user_id=alice, vector_index=0)
    far = make_point(organization_id=organization_id, end_user_id=alice, vector_index=1)
    await seed(store, organization_id, near, far)

    found = await store.search(organization_id, axis(0), end_user_id=alice, min_score=0.5)

    assert [match.id for match in found] == [near.id]


@check
async def a_limit_is_applied(store: FactVectorStore, organization_id: uuid.UUID) -> None:
    alice = uuid.uuid4()
    await seed(
        store,
        organization_id,
        *(
            make_point(organization_id=organization_id, end_user_id=alice, vector_index=0)
            for _ in range(4)
        ),
    )

    found = await store.search(organization_id, axis(0), end_user_id=alice, limit=2)

    assert len(found) == 2


@check
async def the_payload_carries_what_the_spec_names(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """SPEC §6.4's payload, and deliberately no ``text``: the row is the record."""
    alice = uuid.uuid4()
    point = make_point(
        organization_id=organization_id, end_user_id=alice, kind="constraint", confidence=0.75
    )
    await seed(store, organization_id, point)

    found = await store.search(organization_id, axis(0), end_user_id=alice)
    payload = found[0].payload

    assert payload["org_id"] == str(organization_id)
    assert payload["end_user_id"] == str(alice)
    assert payload["kind"] == "constraint"
    assert payload["confidence"] == 0.75
    assert "text" not in payload


@check
async def upserting_the_same_fact_twice_replaces_it(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """The point id *is* the fact id, so an edit overwrites rather than duplicating."""
    alice = uuid.uuid4()
    fact_id = uuid.uuid4()
    await seed(
        store,
        organization_id,
        make_point(organization_id=organization_id, end_user_id=alice, fact_id=fact_id),
    )
    await seed(
        store,
        organization_id,
        make_point(
            organization_id=organization_id,
            end_user_id=alice,
            fact_id=fact_id,
            vector_index=3,
        ),
    )

    assert await store.count(organization_id, end_user_id=alice) == 1
    assert await store.search(organization_id, axis(0), end_user_id=alice, min_score=0.5) == []
    assert len(await store.search(organization_id, axis(3), end_user_id=alice)) == 1


@check
async def deleting_by_id_removes_only_that_fact(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    alice = uuid.uuid4()
    doomed, kept = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        organization_id,
        make_point(organization_id=organization_id, end_user_id=alice, fact_id=doomed),
        make_point(organization_id=organization_id, end_user_id=alice, fact_id=kept),
    )

    await store.delete(organization_id, [doomed])

    found = await store.search(organization_id, axis(0), end_user_id=alice)
    assert [match.id for match in found] == [str(kept)]


@check
async def purging_an_end_user_removes_every_point_and_nobody_elses(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """SPEC §6.5. By filter, not by ids somebody remembered: a purge that missed a point
    nobody knew about would be a right-to-erasure failure."""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    await seed(
        store,
        organization_id,
        make_point(organization_id=organization_id, end_user_id=alice),
        make_point(organization_id=organization_id, end_user_id=alice, vector_index=2),
        make_point(organization_id=organization_id, end_user_id=bob),
    )

    await store.delete_end_user(organization_id, alice)

    assert await store.count(organization_id, end_user_id=alice) == 0
    assert await store.count(organization_id, end_user_id=bob) == 1
    assert await store.search(organization_id, axis(0), end_user_id=alice) == []


@check
async def searching_an_organization_with_no_memory_is_not_an_error(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """The common case for a brand-new customer, and it must not cost an exception."""
    assert await store.dimension(organization_id) is None
    assert await store.search(organization_id, axis(0), end_user_id=uuid.uuid4()) == []
    assert await store.count(organization_id) == 0


@check
async def deleting_from_an_organization_with_no_memory_is_a_no_op(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """A delete asks for an end state, and that end state already holds."""
    await store.delete(organization_id, [uuid.uuid4()])
    await store.delete_end_user(organization_id, uuid.uuid4())


@check
async def the_collection_reports_the_width_it_was_built_with(
    store: FactVectorStore, organization_id: uuid.UUID
) -> None:
    """What recall checks before searching: an index built by a different embedding model
    would otherwise fail silently under ``fail_open``."""
    await store.ensure_collection(organization_id, dimension=DIMENSION)

    assert await store.dimension(organization_id) == DIMENSION


__all__ = ["CHECKS", "DIMENSION", "Check", "axis", "check", "make_point", "seed"]
