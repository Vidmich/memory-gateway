"""The vector-store contract over Chroma, and the translation this backend needs.

Two runs, and they answer different questions.

The **fake** run puts the whole contract through :mod:`tests.chroma_fake`, which speaks
Chroma's dialect — distances, parallel lists, ``where`` operators, offset paging — without
being a vector database. It cannot prove Chroma behaves as assumed; what it proves is that
:mod:`app.services.vector_chroma` translates correctly, which is code in this repository
and should be covered on every run rather than only where a container is available.

The **real** run is marked ``chroma`` and skips without a server. It is the one that
settles whether the assumptions above are true, and the checks it settles are exactly the
ones the contract exists for: what a metadata filter selects, what a delete-by-filter
removes, and whether ``limit`` still means "results" once a filter is involved.

Below the contract runs are the assertions that belong to this backend alone, because they
are about the translation rather than about the port: the distance conversion, the
over-fetch a client-side score floor requires, the payload round trip including nulls, and
the embedding function that must never be Chroma's own.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.config import get_settings
from app.services.vector_chroma import (
    ABSENT_KEY,
    DIMENSION_KEY,
    OVERFETCH,
    ChromaFactVectorStore,
    ChromaVectorIndexAdmin,
    ChromaVectorStore,
    _decode,
    _encode,
)
from app.services.vector_index import InMemoryLiveCollections
from app.services.vector_store import ChunkPoint, VectorStore, point_id
from tests.chroma_fake import FakeChromaClient
from tests.fact_vector_store_contract import CHECKS as FACT_CHECKS
from tests.vector_store_contract import CHECKS, DIMENSION, axis


def fake_store() -> tuple[ChromaVectorStore, FakeChromaClient]:
    client = FakeChromaClient()
    return ChromaVectorStore(client, live=InMemoryLiveCollections()), client


@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_the_translation_satisfies_the_contract(name: str) -> None:
    store, _ = fake_store()
    await CHECKS[name](store, uuid.uuid4())


@pytest.mark.parametrize("name", sorted(FACT_CHECKS), ids=str)
async def test_the_memory_translation_satisfies_the_contract(name: str) -> None:
    await FACT_CHECKS[name](ChromaFactVectorStore(FakeChromaClient()), uuid.uuid4())


# ---------------------------------------------------------------------------
# the real thing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chroma_available() -> bool:
    """One synchronous reachability check for the whole module, as the Qdrant runner does.

    Not an async client fixture, for the same reason stated there: pytest-asyncio gives
    each test its own event loop, and a client built in a module-scoped one cannot be used
    from a function-scoped one.
    """
    settings = get_settings()
    url = settings.chroma_url
    if not url:
        pytest.skip("CHROMA_URL is not configured")
    try:
        response = httpx.get(f"{url}/api/v2/heartbeat", timeout=2.0)
        response.raise_for_status()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"chroma not available: {exc}")
    return True


@pytest.fixture
async def chroma_store(chroma_available: bool) -> AsyncIterator[VectorStore]:
    import chromadb

    settings = get_settings()
    parsed = httpx.URL(settings.chroma_url or "")
    client = await chromadb.AsyncHttpClient(
        host=parsed.host, port=parsed.port or 8000, ssl=parsed.scheme == "https"
    )
    yield ChromaVectorStore(client, live=InMemoryLiveCollections())


@pytest.mark.chroma
@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_chroma_satisfies_the_contract(name: str, chroma_store: VectorStore) -> None:
    organization_id = uuid.uuid4()
    try:
        await CHECKS[name](chroma_store, organization_id)
    finally:
        await chroma_store.drop(organization_id)


# ---------------------------------------------------------------------------
# the translation itself
# ---------------------------------------------------------------------------


async def test_no_collection_is_ever_opened_with_chromas_own_embedding_function() -> None:
    """The trap named in the module docstring, asserted rather than intended.

    Chroma's default embedding function downloads an ONNX model on first use. In task 18's
    image that fails outright; if it ever succeeded it would embed queries with a model
    unrelated to the one that built the index, which is worse than failing. So the check is
    not "the code passes None somewhere" but "None reached the client on every call that
    creates or opens a collection".
    """
    store, client = fake_store()
    organization = uuid.uuid4()
    await store.ensure_collection(organization, dimension=DIMENSION)
    await store.search(organization, axis(0))

    opened = client.opened_with("get_or_create_collection") + client.opened_with("get_collection")

    assert opened, "no collection was opened, so the assertion would be vacuous"
    assert all(value is None for value in opened)


async def test_the_collection_is_created_for_cosine_and_records_its_width() -> None:
    """Chroma's default space is L2, which for embeddings ranks by length rather than
    direction — plausible-looking results, consistently wrong. And the width has to be
    written down because Chroma infers it and will not report it back."""
    store, client = fake_store()
    organization = uuid.uuid4()

    await store.ensure_collection(organization, dimension=DIMENSION)

    [created] = [call for call in client.calls if call.method == "get_or_create_collection"]
    assert created.kwargs["configuration"] == {"hnsw": {"space": "cosine"}}
    assert created.kwargs["metadata"][DIMENSION_KEY] == DIMENSION
    assert await store.dimension(organization) == DIMENSION


async def test_a_score_floor_over_fetches_rather_than_narrowing_the_result() -> None:
    """A client-side floor must not eat into the caller's ``limit``.

    Without the over-fetch, asking for five results above a threshold would ask the server
    for five candidates and then return however many survived — so the number of chunks a
    prompt gets would depend on how many weak matches happened to rank above them.
    """
    store, client = fake_store()
    organization = uuid.uuid4()
    await store.ensure_collection(organization, dimension=DIMENSION)

    await store.search(organization, axis(0), limit=5, min_score=0.5)
    await store.search(organization, axis(0), limit=5)

    floored, plain = [call for call in client.calls if call.method == "query"]
    assert floored.kwargs["n_results"] == 5 * OVERFETCH
    assert plain.kwargs["n_results"] == 5


async def test_a_payload_survives_the_round_trip_including_its_nulls() -> None:
    """``page_or_section`` is ``None`` for a document with no structure, and that is a
    fact about the chunk rather than a field the store may drop. An omitted key and a key
    holding ``None`` are different things to the citation that reads it."""
    payload = {
        "org_id": "o",
        "document_id": "d",
        "page_or_section": None,
        "chunk_index": 3,
        "text": "hello",
    }

    text, metadata = _encode(payload)

    assert text == "hello"
    assert "page_or_section" not in metadata
    assert metadata[ABSENT_KEY] == "page_or_section"
    assert _decode(text, metadata) == payload


async def test_the_stored_width_never_leaks_into_a_payload() -> None:
    """The dimension is collection metadata, not chunk metadata — but a fake server that
    merged the two, or a future version that echoes collection metadata on a point, would
    put a ``gateway_dimension`` key into every citation."""
    assert DIMENSION_KEY not in _decode("t", {"org_id": "o", DIMENSION_KEY: 8})


async def test_a_pointer_to_a_deleted_collection_reads_as_nothing_indexed() -> None:
    """Somebody deletes a collection by hand. Every read should answer "nothing indexed"
    and the next ingestion should heal it — not raise on the retrieval path."""
    client = FakeChromaClient()
    live = InMemoryLiveCollections()
    store = ChromaVectorStore(client, live=live)
    admin = ChromaVectorIndexAdmin(client, live=live)
    organization = uuid.uuid4()
    await store.ensure_collection(organization, dimension=DIMENSION)

    client.collections.clear()

    assert await admin.live_collection(organization) is None
    assert await store.search(organization, axis(0)) == []
    assert await store.count(organization) == 0
    assert await store.dimension(organization) is None


async def test_promotion_is_one_pointer_write() -> None:
    """Chroma has no alias, so the property the port asks for — a reader sees the old
    collection or the new one, never neither — comes from the pointer being written once
    rather than from two renames with a gap between them."""
    client = FakeChromaClient()
    live = InMemoryLiveCollections()
    store = ChromaVectorStore(client, live=live)
    admin = ChromaVectorIndexAdmin(client, live=live)
    organization = uuid.uuid4()
    await store.ensure_collection(organization, dimension=DIMENSION)
    rebuilt = f"org_{organization}_docs_v2"
    await admin.create_collection(rebuilt, dimension=DIMENSION)
    await admin.upsert_into(
        rebuilt,
        [ChunkPoint(id=point_id(uuid.uuid4(), 0), vector=axis(0), payload={"text": "new"})],
    )

    before = await store.search(organization, axis(0))
    await admin.promote(organization, rebuilt)
    after = await store.search(organization, axis(0))

    assert before == []
    assert [match.text for match in after] == ["new"]
    assert await admin.live_collection(organization) == rebuilt


async def test_scrolling_carries_vectors_only_when_asked() -> None:
    """A reindex re-embeds from payload text and wants none of the old vectors; a
    migration between backends wants exactly them and nothing else."""
    client = FakeChromaClient()
    admin = ChromaVectorIndexAdmin(client, live=InMemoryLiveCollections())
    await admin.create_collection("c", dimension=DIMENSION)
    await admin.upsert_into(
        "c", [ChunkPoint(id="a", vector=axis(1), payload={"text": "x", "document_id": "d"})]
    )

    without = await admin.scroll("c", cursor=None, limit=10)
    with_vectors = await admin.scroll("c", cursor=None, limit=10, with_vectors=True)

    assert without.points[0].vector == []
    assert with_vectors.points[0].vector == axis(1)


async def test_scrolling_pages_by_offset_and_stops_at_the_end() -> None:
    """The cursor is an offset here rather than a point id. It stays opaque to the caller,
    and the last page reports no cursor rather than an offset past the end — otherwise a
    copy loop would page forever over an empty result."""
    client = FakeChromaClient()
    admin = ChromaVectorIndexAdmin(client, live=InMemoryLiveCollections())
    await admin.create_collection("c", dimension=DIMENSION)
    await admin.upsert_into(
        "c",
        [
            ChunkPoint(id=f"p{index}", vector=axis(0), payload={"text": str(index)})
            for index in range(5)
        ],
    )

    first = await admin.scroll("c", cursor=None, limit=2)
    second = await admin.scroll("c", cursor=first.cursor, limit=2)
    third = await admin.scroll("c", cursor=second.cursor, limit=2)

    assert [len(page.points) for page in (first, second, third)] == [2, 2, 1]
    assert third.cursor is None
    assert {point.id for page in (first, second, third) for point in page.points} == {
        f"p{index}" for index in range(5)
    }
