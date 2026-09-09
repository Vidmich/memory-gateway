"""The vector-store contract, run against memory and against a real Qdrant.

The Qdrant half is marked ``qdrant`` and skips when nothing is listening. The task file
is explicit that it should not be a mock, and the reason is visible in the contract: the
checks are about payload filters and delete-by-filter, which is precisely where a
hand-written double agrees with itself.

Each check gets its own organization id, and therefore its own collection, which is
dropped afterwards. That is not tidiness — the collection name *is* the tenant boundary,
so sharing one between checks would test the opposite of what SPEC §5.3 asks for.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.config import get_settings
from app.services.vector_qdrant import QdrantVectorStore
from app.services.vector_store import MemoryVectorStore, VectorStore
from tests.vector_store_contract import CHECKS


@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_the_memory_store_satisfies_the_contract(name: str) -> None:
    await CHECKS[name](MemoryVectorStore(), uuid.uuid4())


@pytest.fixture(scope="module")
def qdrant_available() -> bool:
    """One synchronous reachability check for the whole module.

    Deliberately not an async client fixture: pytest-asyncio gives each test its own event
    loop, and a client created in a module-scoped loop cannot be used from a function
    one. Probing over plain HTTP costs one request and lets each test build its own
    client in its own loop, which is also how the application does it.
    """
    settings = get_settings()
    try:
        response = httpx.get(f"{settings.qdrant_url}/collections", timeout=2.0)
        response.raise_for_status()
    except Exception as exc:
        pytest.skip(f"qdrant not available: {exc}")
    return True


@pytest.fixture
async def qdrant_store(qdrant_available: bool) -> AsyncIterator[VectorStore]:
    from qdrant_client import AsyncQdrantClient

    settings = get_settings()
    client = AsyncQdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key, check_compatibility=False
    )
    try:
        yield QdrantVectorStore(client)
    finally:
        await client.close()


@pytest.mark.qdrant
@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_qdrant_satisfies_the_contract(name: str, qdrant_store: VectorStore) -> None:
    organization_id = uuid.uuid4()
    try:
        await CHECKS[name](qdrant_store, organization_id)
    finally:
        await qdrant_store.drop(organization_id)
