"""Object storage, behind a port.

Everything here is an ``AsyncIterator[bytes]`` in and an ``AsyncIterator[bytes]`` out.
That is the whole design, and it is what makes "a 50 MB file does not exhaust worker
memory" a property of the interface rather than a thing every caller has to remember: a
port that handed out ``bytes`` would make the buffered implementation the easy one and
the streaming one an act of discipline.

boto3 is synchronous, so every call that touches a socket runs in a worker thread. A
blocking S3 read on the event loop stalls every other request on the process, and an
upload of a large file would stall it for seconds.

The upload path is a real multipart upload once the body passes the part size, not a
buffered ``put_object``. It also enforces the size cap *while reading* rather than after:
the point of a cap is to not store the bytes, and a check that runs after the last one
has arrived has already lost.

:class:`MemoryObjectStore` is not a mock — it is the second implementation, and
``tests/object_store_contract.py`` runs the same assertions against both, so a test that
passes here means the same thing it would against MinIO.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.core.errors import AppError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = Any

logger = logging.getLogger(__name__)

#: S3 requires every part except the last to be at least 5 MiB. Eight is a round number
#: above that, and it is also the ceiling on how much of a body is ever in memory at once.
PART_SIZE_BYTES = 8 * 1024 * 1024

#: How much is read at a time when streaming an object back out. Small enough that a
#: pathological file is never resident, large enough that a 50 MB read is not 50 000
#: thread hops.
READ_CHUNK_BYTES = 256 * 1024

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class ObjectTooLarge(AppError):
    """The body exceeded the per-file cap.

    A 413 rather than a 422: the request was well formed, there was simply too much of
    it, and clients retry the two differently.
    """

    status_code = 413
    code = "object_too_large"


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """One object as a *listing* sees it — no body, and no request per entry.

    ``etag`` is whatever the store calls a version. It is compared, never parsed: it is
    an MD5 for a single-part S3 upload and something else entirely for a multipart one,
    and code that assumed the first would break on exactly the large files this task
    exists to handle.
    """

    key: str
    size_bytes: int
    etag: str | None = None
    modified_at: datetime | None = None

    @property
    def name(self) -> str:
        """The last path segment — what a person calls the file."""
        return self.key.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    etag: str | None = None


class ObjectStore(Protocol):
    """The bytes half of a connector. Deliberately smaller than S3."""

    async def put(
        self,
        key: str,
        body: AsyncIterator[bytes],
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
        max_bytes: int | None = None,
    ) -> StoredObject:
        """Stream ``body`` to ``key``.

        Raises :class:`ObjectTooLarge` as soon as ``max_bytes`` is passed, leaving nothing
        behind — a partially written object under a key a customer will later list is
        worse than a failed upload.
        """
        ...

    def open(self, key: str) -> AsyncIterator[bytes]:
        """Stream the object back. Raises :class:`KeyError` if it is not there."""
        ...

    def list(self, prefix: str) -> AsyncIterator[ObjectRef]:
        """Every object under ``prefix``, paged. Order is the store's, not ours."""
        ...

    async def head(self, key: str) -> ObjectRef | None: ...

    async def delete(self, keys: Sequence[str]) -> int:
        """Delete by key, returning how many were removed. Missing keys are not an
        error: a delete is a statement about the end state, and a resync that raced a
        manual deletion should finish, not fail."""
        ...

    async def delete_prefix(self, prefix: str) -> int: ...

    def presign_put(self, key: str, *, expires_in: int, content_type: str | None = None) -> str:
        """A short-lived URL a customer can ``PUT`` to directly.

        Synchronous because it signs rather than calls: there is no round trip, and
        making it a coroutine would suggest otherwise.
        """
        ...


# ---------------------------------------------------------------------------
# S3 / MinIO
# ---------------------------------------------------------------------------


class S3ObjectStore:
    """The production implementation, over the same boto3 client `/readyz` probes."""

    def __init__(self, client: S3Client, bucket: str, *, part_size: int = PART_SIZE_BYTES) -> None:
        self._client = client
        self._bucket = bucket
        self._part_size = part_size

    async def put(
        self,
        key: str,
        body: AsyncIterator[bytes],
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
        max_bytes: int | None = None,
    ) -> StoredObject:
        buffer = bytearray()
        total = 0
        upload_id: str | None = None
        parts: list[Any] = []

        try:
            async for piece in body:
                total += len(piece)
                if max_bytes is not None and total > max_bytes:
                    raise ObjectTooLarge(
                        f"This file is larger than the {_megabytes(max_bytes)} MB limit."
                    )
                buffer.extend(piece)
                # Flush whole parts only. The tail is kept because S3 rejects a
                # non-final part below 5 MiB, and we do not know yet whether this is one.
                while len(buffer) > self._part_size:
                    if upload_id is None:
                        upload_id = await self._start(key, content_type)
                    parts.append(await self._upload_part(key, upload_id, len(parts) + 1, buffer))
                    del buffer[: self._part_size]

            if upload_id is None:
                return await self._put_single(key, bytes(buffer), content_type)

            parts.append(await self._upload_part(key, upload_id, len(parts) + 1, buffer, last=True))
            etag = await asyncio.to_thread(
                lambda: self._client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                ).get("ETag")
            )
            return StoredObject(key=key, size_bytes=total, etag=_clean_etag(etag))
        except BaseException:
            # Including cancellation: an abandoned multipart upload is billed storage
            # that no listing shows, which is the worst kind of leak to find later.
            if upload_id is not None:
                await self._abort(key, upload_id)
            raise

    async def _put_single(self, key: str, data: bytes, content_type: str) -> StoredObject:
        response = await asyncio.to_thread(
            lambda: self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        )
        return StoredObject(key=key, size_bytes=len(data), etag=_clean_etag(response.get("ETag")))

    async def _start(self, key: str, content_type: str) -> str:
        response = await asyncio.to_thread(
            lambda: self._client.create_multipart_upload(
                Bucket=self._bucket, Key=key, ContentType=content_type
            )
        )
        return str(response["UploadId"])

    async def _upload_part(
        self, key: str, upload_id: str, number: int, buffer: bytearray, *, last: bool = False
    ) -> Any:
        data = bytes(buffer) if last else bytes(buffer[: self._part_size])
        response = await asyncio.to_thread(
            lambda: self._client.upload_part(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=data,
            )
        )
        return {"ETag": response["ETag"], "PartNumber": number}

    async def _abort(self, key: str, upload_id: str) -> None:
        try:
            await asyncio.to_thread(
                lambda: self._client.abort_multipart_upload(
                    Bucket=self._bucket, Key=key, UploadId=upload_id
                )
            )
        except Exception:  # the original failure is the one worth raising
            logger.warning("failed to abort multipart upload", extra={"key": key}, exc_info=True)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        try:
            response = await asyncio.to_thread(
                lambda: self._client.get_object(Bucket=self._bucket, Key=key)
            )
        except self._client.exceptions.NoSuchKey as exc:
            raise KeyError(key) from exc
        stream = response["Body"]
        try:
            while True:
                piece = await asyncio.to_thread(stream.read, READ_CHUNK_BYTES)
                if not piece:
                    return
                yield piece
        finally:
            await asyncio.to_thread(stream.close)

    async def list(self, prefix: str) -> AsyncIterator[ObjectRef]:
        token: str | None = None
        while True:
            arguments: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                arguments["ContinuationToken"] = token
            page = await asyncio.to_thread(
                lambda query=arguments: self._client.list_objects_v2(**query)  # type: ignore[misc]
            )
            for entry in page.get("Contents", []):
                key = str(entry["Key"])
                # A folder-shaped zero-byte marker is not a document. Ingesting one
                # produces an empty `failed` row for something nobody uploaded.
                if key.endswith("/"):
                    continue
                yield ObjectRef(
                    key=key,
                    size_bytes=int(entry.get("Size", 0)),
                    etag=_clean_etag(entry.get("ETag")),
                    modified_at=entry.get("LastModified"),
                )
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return

    async def head(self, key: str) -> ObjectRef | None:
        try:
            response = await asyncio.to_thread(
                lambda: self._client.head_object(Bucket=self._bucket, Key=key)
            )
        except Exception:
            return None
        return ObjectRef(
            key=key,
            size_bytes=int(response.get("ContentLength", 0)),
            etag=_clean_etag(response.get("ETag")),
            modified_at=response.get("LastModified"),
        )

    async def delete(self, keys: Sequence[str]) -> int:
        removed = 0
        for batch in _batched(keys, 1000):  # the DeleteObjects limit
            await asyncio.to_thread(
                lambda objects=batch: self._client.delete_objects(  # type: ignore[misc]
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": key} for key in objects], "Quiet": True},
                )
            )
            removed += len(batch)
        return removed

    async def delete_prefix(self, prefix: str) -> int:
        keys = [ref.key async for ref in self.list(prefix)]
        return await self.delete(keys)

    def presign_put(self, key: str, *, expires_in: int, content_type: str | None = None) -> str:
        parameters: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if content_type:
            parameters["ContentType"] = content_type
        return self._client.generate_presigned_url(
            "put_object", Params=parameters, ExpiresIn=expires_in
        )


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    data: bytes
    content_type: str
    modified_at: datetime


@dataclass
class MemoryObjectStore:
    """The same store without a socket.

    Its ETag is a content hash, which is what S3 does for a single-part upload and is
    good enough to reconcile against — the contract test asserts only that re-putting
    different bytes changes it, because that is all any caller may rely on.
    """

    objects: dict[str, _Entry] = field(default_factory=dict)
    #: Presigned URLs are not reachable here. The value is still a URL so a test can
    #: assert on its shape, and the key is echoed so the caller can prove which object
    #: it was signed for.
    presign_base: str = "https://storage.test/presigned"

    async def put(
        self,
        key: str,
        body: AsyncIterator[bytes],
        *,
        content_type: str = DEFAULT_CONTENT_TYPE,
        max_bytes: int | None = None,
    ) -> StoredObject:
        buffer = bytearray()
        async for piece in body:
            buffer.extend(piece)
            if max_bytes is not None and len(buffer) > max_bytes:
                raise ObjectTooLarge(
                    f"This file is larger than the {_megabytes(max_bytes)} MB limit."
                )
        data = bytes(buffer)
        self.objects[key] = _Entry(
            data=data, content_type=content_type, modified_at=datetime.now(UTC)
        )
        return StoredObject(key=key, size_bytes=len(data), etag=_hash_etag(data))

    async def open(self, key: str) -> AsyncIterator[bytes]:
        entry = self.objects.get(key)
        if entry is None:
            raise KeyError(key)
        # Yielded in pieces rather than whole, so a caller that only works because the
        # body arrived in one go fails here as it would against S3.
        for start in range(0, max(len(entry.data), 1), READ_CHUNK_BYTES):
            piece = entry.data[start : start + READ_CHUNK_BYTES]
            if piece:
                yield piece

    async def list(self, prefix: str) -> AsyncIterator[ObjectRef]:
        for key in sorted(self.objects):
            if key.startswith(prefix) and not key.endswith("/"):
                yield await self._ref(key)

    async def head(self, key: str) -> ObjectRef | None:
        if key not in self.objects:
            return None
        return await self._ref(key)

    async def _ref(self, key: str) -> ObjectRef:
        entry = self.objects[key]
        return ObjectRef(
            key=key,
            size_bytes=len(entry.data),
            etag=_hash_etag(entry.data),
            modified_at=entry.modified_at,
        )

    async def delete(self, keys: Sequence[str]) -> int:
        return sum(1 for key in keys if self.objects.pop(key, None) is not None)

    async def delete_prefix(self, prefix: str) -> int:
        return await self.delete([key for key in list(self.objects) if key.startswith(prefix)])

    def presign_put(self, key: str, *, expires_in: int, content_type: str | None = None) -> str:
        return f"{self.presign_base}/{key}?expires={expires_in}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hash_etag(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _clean_etag(value: str | None) -> str | None:
    """S3 returns the ETag wrapped in literal quote characters."""
    return value.strip('"') if value else None


def _megabytes(value: int) -> int:
    return max(1, value // (1024 * 1024))


def _batched[T](values: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "PART_SIZE_BYTES",
    "MemoryObjectStore",
    "ObjectRef",
    "ObjectStore",
    "ObjectTooLarge",
    "S3ObjectStore",
    "StoredObject",
]
