"""The object-store contract, run against memory and against MinIO.

The MinIO half is marked ``s3`` and skips when nothing is listening. It is not optional
in CI: the checks that matter — multipart upload, prefix listing, delete-by-prefix — are
exactly the ones a hand-written double would get right by agreeing with itself.

The S3 store under test is built with a deliberately tiny part size. The alternative is a
16 MB upload in a unit test, and a threshold crossed at 32 KB exercises precisely the same
code path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.config import get_settings
from app.services.object_store import MemoryObjectStore, ObjectStore, S3ObjectStore
from tests.object_store_contract import CHECKS


def prefix() -> str:
    """A fresh prefix per check, so a real bucket does not carry state between them."""
    return f"tests/{uuid.uuid4()}/"


@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_the_memory_store_satisfies_the_contract(name: str) -> None:
    await CHECKS[name](MemoryObjectStore(), prefix())


@pytest.fixture(scope="module")
def s3_client() -> Iterator[Any]:
    """Session-lived, and the reachability check is paid **once**.

    Per-test would mean boto3's connect timeout and retries for every one of the sixteen
    checks — around four minutes of a suite that is otherwise seconds, on a laptop with
    nothing running.
    """
    settings = get_settings()
    from app.core.clients import create_storage_client

    client = create_storage_client(settings)
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except Exception as exc:  # no MinIO, wrong credentials, no bucket
        client.close()
        pytest.skip(f"object storage not available: {exc}")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def s3_store(s3_client: Any) -> ObjectStore:
    # 32 KB parts: the multipart check crosses the threshold with a 64 KB body instead of
    # a 16 MB one, through exactly the same code.
    return S3ObjectStore(s3_client, get_settings().s3_bucket, part_size=32 * 1024)


@pytest.mark.s3
@pytest.mark.parametrize("name", sorted(CHECKS), ids=str)
async def test_the_s3_store_satisfies_the_contract(name: str, s3_store: ObjectStore) -> None:
    where = prefix()
    try:
        await CHECKS[name](s3_store, where)
    finally:
        await s3_store.delete_prefix(where)
        await s3_store.delete_prefix(f"{where.rstrip('/')}-other/")
