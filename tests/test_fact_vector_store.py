"""The fact-vector contract, run against memory and against a real Qdrant.

Same arrangement as ``tests/test_vector_store.py``: the Qdrant half is marked ``qdrant``
and skips when nothing is listening, and each check gets its own organization id — and
therefore its own collection — which is dropped afterwards. Sharing one between checks
would test the opposite of what SPEC §5.3 asks for, since the collection name *is* the
tenant boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.config import get_settings
from app.services.fact_vectors import (
    FactVectorStore,
    MemoryFactVectorStore,
    QdrantFactVectorStore,
    memory_collection_for,
)
from tests.fact_vector_store_contract import CHECKS


def test_the_collection_is_named_per_tenant() -> None:
    """SPEC §6.4's name, and separate from the document collection: a query that reaches
    the wrong one finds nothing rather than somebody else's."""
    organization_id = uuid.UUID(int=1)

    assert memory_collection_for(organization_id) == f"org_{organization_id}_memory"


@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_the_memory_store_satisfies_the_contract(name: str) -> None:
    await CHECKS[name](MemoryFactVectorStore(), uuid.uuid4())


@pytest.fixture(scope="module")
def qdrant_available() -> bool:
    settings = get_settings()
    try:
        response = httpx.get(f"{settings.qdrant_url}/collections", timeout=2.0)
        response.raise_for_status()
    except Exception as exc:
        pytest.skip(f"qdrant not available: {exc}")
    return True


@pytest.fixture
async def qdrant_store(qdrant_available: bool) -> AsyncIterator[FactVectorStore]:
    from qdrant_client import AsyncQdrantClient

    settings = get_settings()
    client = AsyncQdrantClient(
        url=settings.qdrant_url, api_key=settings.qdrant_api_key, check_compatibility=False
    )
    try:
        yield QdrantFactVectorStore(client)
    finally:
        await client.close()


@pytest.mark.qdrant
@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_qdrant_satisfies_the_contract(name: str, qdrant_store: FactVectorStore) -> None:
    organization_id = uuid.uuid4()
    try:
        await CHECKS[name](qdrant_store, organization_id)
    finally:
        await qdrant_store.drop(organization_id)
