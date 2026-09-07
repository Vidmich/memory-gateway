"""One set of assertions, run against the memory store and against MinIO.

Same shape and same reasons as ``tests/gateway_store_contract.py``: the memory
implementation is fast enough to run on every commit, and the only thing that makes it
*trustworthy* is that the identical checks pass against the real one.

The checks worth having here are the ones a hand-written double would get wrong by
agreeing with itself — streaming a body larger than the multipart threshold, listing with
a prefix that has a sibling, deleting a key that is already gone. Those are exactly where
S3 has opinions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from app.services.object_store import ObjectStore, ObjectTooLarge

#: Larger than :data:`~app.services.object_store.PART_SIZE_BYTES` would be, if the store
#: under test is configured with a small one. The S3 fixture shrinks the part size rather
#: than uploading 16 MB, so this stays a fast test that still crosses the threshold.
BIG = b"0123456789abcdef" * 4096  # 64 KiB

Check = Callable[[ObjectStore, str], Awaitable[None]]
CHECKS: dict[str, Check] = {}


def check(function: Check) -> Check:
    CHECKS[function.__name__] = function
    return function


async def stream(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


async def read(store: ObjectStore, key: str) -> bytes:
    return b"".join([piece async for piece in store.open(key)])


@check
async def a_stored_object_reads_back_byte_for_byte(store: ObjectStore, prefix: str) -> None:
    await store.put(f"{prefix}notes.md", stream(b"# hello\n", b"world\n"))

    assert await read(store, f"{prefix}notes.md") == b"# hello\nworld\n"


@check
async def a_body_larger_than_one_part_round_trips(store: ObjectStore, prefix: str) -> None:
    """The multipart path. A buffered implementation passes every other check here and
    fails this one at whatever size the threshold happens to be."""
    await store.put(
        f"{prefix}big.bin", stream(*[BIG[i : i + 7000] for i in range(0, len(BIG), 7000)])
    )

    assert await read(store, f"{prefix}big.bin") == BIG


@check
async def an_empty_object_is_stored_and_read_back(store: ObjectStore, prefix: str) -> None:
    stored = await store.put(f"{prefix}empty.txt", stream())

    assert stored.size_bytes == 0
    assert await read(store, f"{prefix}empty.txt") == b""


@check
async def the_reported_size_is_the_real_size(store: ObjectStore, prefix: str) -> None:
    stored = await store.put(f"{prefix}sized.bin", stream(b"a" * 100, b"b" * 250))

    assert stored.size_bytes == 350


@check
async def reading_a_missing_key_raises(store: ObjectStore, prefix: str) -> None:
    with pytest.raises(KeyError):
        await read(store, f"{prefix}not-there.md")


@check
async def a_listing_returns_only_this_prefix(store: ObjectStore, prefix: str) -> None:
    """The isolation guarantee for objects is a string comparison, so this is the check
    that a sibling prefix does not leak into it."""
    await store.put(f"{prefix}a.md", stream(b"a"))
    await store.put(f"{prefix}nested/b.md", stream(b"b"))
    await store.put(f"{prefix.rstrip('/')}-other/c.md", stream(b"c"))

    keys = {ref.key async for ref in store.list(prefix)}

    assert keys == {f"{prefix}a.md", f"{prefix}nested/b.md"}


@check
async def a_listing_carries_sizes_and_etags(store: ObjectStore, prefix: str) -> None:
    await store.put(f"{prefix}sized.md", stream(b"x" * 42))

    [ref] = [ref async for ref in store.list(prefix)]

    assert ref.size_bytes == 42
    assert ref.etag
    assert ref.name == "sized.md"


@check
async def changing_the_bytes_changes_the_etag(store: ObjectStore, prefix: str) -> None:
    """All resync relies on. Not "the ETag is an MD5" — it is not, for a multipart
    upload — only that different content compares unequal."""
    first = await store.put(f"{prefix}f.md", stream(b"one"))
    second = await store.put(f"{prefix}f.md", stream(b"two"))

    assert first.etag != second.etag


@check
async def rewriting_a_key_replaces_it(store: ObjectStore, prefix: str) -> None:
    await store.put(f"{prefix}f.md", stream(b"first"))
    await store.put(f"{prefix}f.md", stream(b"second"))

    assert await read(store, f"{prefix}f.md") == b"second"
    assert len([ref async for ref in store.list(prefix)]) == 1


@check
async def head_describes_an_object_without_reading_it(store: ObjectStore, prefix: str) -> None:
    await store.put(f"{prefix}f.md", stream(b"x" * 10))

    ref = await store.head(f"{prefix}f.md")

    assert ref is not None and ref.size_bytes == 10
    assert await store.head(f"{prefix}missing.md") is None


@check
async def deleting_a_missing_key_is_not_an_error(store: ObjectStore, prefix: str) -> None:
    """A delete states an end state. A resync racing a manual deletion should finish."""
    await store.delete([f"{prefix}never-existed.md"])


@check
async def deleting_a_prefix_removes_everything_under_it(store: ObjectStore, prefix: str) -> None:
    await store.put(f"{prefix}a.md", stream(b"a"))
    await store.put(f"{prefix}deep/b.md", stream(b"b"))
    await store.put(f"{prefix.rstrip('/')}-other/c.md", stream(b"c"))

    await store.delete_prefix(prefix)

    assert [ref async for ref in store.list(prefix)] == []
    assert [ref async for ref in store.list(f"{prefix.rstrip('/')}-other/")] != []


@check
async def a_body_over_the_cap_is_refused(store: ObjectStore, prefix: str) -> None:
    with pytest.raises(ObjectTooLarge):
        await store.put(f"{prefix}huge.bin", stream(b"x" * 5000), max_bytes=1024)


@check
async def a_refused_body_leaves_nothing_behind(store: ObjectStore, prefix: str) -> None:
    """A partially written object under a key a customer will later list is worse than a
    failed upload — a resync would find it and index half a file."""
    with pytest.raises(ObjectTooLarge):
        await store.put(f"{prefix}huge.bin", stream(*[b"x" * 4096] * 40), max_bytes=8192)

    assert [ref async for ref in store.list(prefix)] == []


@check
async def a_presigned_url_names_the_key(store: ObjectStore, prefix: str) -> None:
    url = store.presign_put(f"{prefix}scripted.md", expires_in=900)

    assert url.startswith("http")
    assert "scripted.md" in url


async def run_checks(store: ObjectStore, prefix: str, name: str) -> None:
    await CHECKS[name](store, prefix)


__all__ = ["BIG", "CHECKS", "read", "run_checks", "stream"]
