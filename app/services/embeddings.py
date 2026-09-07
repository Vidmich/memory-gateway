"""Turning chunks into vectors (SPEC §9.4).

One embedding model per *platform*, not per organization. That is a constraint, not a
simplification: a Qdrant collection's vectors must all come from the same model, and two
tenants on different models would either need two collections each or would silently
retrieve nonsense. Task 17 moves the configuration from environment variables into
``platform_settings`` and adds the reindex-and-swap flow; the shape here is what it will
read.

Three things this module is careful about, all of them about the provider being a remote
service that occasionally says no.

**Batching.** One request per chunk turns a 400-chunk document into 400 round trips.
Batches are sized by count *and* by token budget, because a provider rejects an oversized
batch with a 400, and a 400 is not retryable.

**Bounded concurrency.** Batches run in parallel, but not unboundedly: a single large
document should not open forty connections and earn the whole deployment a rate limit.

**Retry on 429 and 5xx, honouring ``Retry-After``.** The header is the provider telling
us exactly how long to wait, and ignoring it in favour of our own backoff is how a
throttled client stays throttled.

:class:`HashEmbedder` is the fourth thing, and it is not a test double. It is a real
hashing-trick bag-of-words embedder: a query that shares words with a chunk scores above
one that does not. It is *lexical only* — it knows nothing about meaning, so "car" and
"automobile" are unrelated to it — which makes it the wrong choice for production and the
right one for a development stack with no provider key, where the alternative is that
nothing about ingestion can be demonstrated at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.errors import AppError

logger = logging.getLogger(__name__)

#: Chunks per request. Providers accept more, but a batch that fails takes everything in
#: it down with it, and a hundred is already two orders of magnitude better than one.
DEFAULT_BATCH_SIZE = 96

#: Rough token ceiling for one request. Counted in characters over four, because the exact
#: number does not matter — this exists to stay clear of the provider's own limit, not to
#: sit exactly on it.
MAX_BATCH_CHARS = 200_000

DEFAULT_MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 20.0


class EmbeddingError(AppError):
    """The provider could not embed this batch. Reaches ``documents.error`` for a
    permanent failure and is retried for a transient one — :attr:`retryable` is which."""

    status_code = 502
    code = "embedding_failed"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class Embedder(Protocol):
    @property
    def model(self) -> str:
        """Recorded on every document, so a platform model change shows up as drift."""
        ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per input, in the input's order.

        Order is part of the contract because the caller pairs the result with chunk
        indexes positionally; a provider that reorders its output is why the OpenAI
        implementation sorts by the ``index`` field instead of trusting the array.
        """
        ...


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """What it takes to call an embedding provider. Task 17 loads this from the database
    instead of the environment; nothing downstream has to change when it does."""

    provider: str
    model: str
    dimension: int
    base_url: str = ""
    api_key: str | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    max_concurrency: int = 4
    timeout_seconds: float = 60.0

    def __repr__(self) -> str:
        """Redacted. A dataclass repr is what ends up in a traceback, a log line, or a
        debugger session, and SPEC §5.4 does not carve out an exception for any of them."""
        return (
            f"EmbeddingSettings(provider={self.provider!r}, model={self.model!r}, "
            f"dimension={self.dimension}, base_url={self.base_url!r}, "
            f"api_key={'set' if self.api_key else None!r})"
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible
# ---------------------------------------------------------------------------


class OpenAIEmbedder:
    """Anything that speaks ``POST /embeddings`` — OpenAI, Azure, vLLM, Ollama, TEI."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: EmbeddingSettings,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._client = client
        self._settings = settings
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._gate = asyncio.Semaphore(max(1, settings.max_concurrency))

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def dimension(self) -> int:
        return self._settings.dimension

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        batches = list(_batches(texts, self._settings.batch_size))
        results = await asyncio.gather(*(self._embed_batch(batch) for batch in batches))
        return [vector for batch in results for vector in batch]

    async def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        async with self._gate:
            for attempt in range(1, self._max_attempts + 1):
                try:
                    return await self._call(texts)
                except EmbeddingError as error:
                    if not error.retryable or attempt == self._max_attempts:
                        raise
                    delay = _backoff(attempt, getattr(error, "retry_after", None))
                    logger.info(
                        "retrying embedding batch",
                        extra={"attempt": attempt, "delay_seconds": round(delay, 2)},
                    )
                    await self._sleep(delay)
            raise EmbeddingError("embedding retries exhausted", retryable=True)  # unreachable

    async def _call(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"content-type": "application/json"}
        if self._settings.api_key:
            headers["authorization"] = f"Bearer {self._settings.api_key}"

        try:
            response = await self._client.post(
                f"{self._settings.base_url.rstrip('/')}/embeddings",
                json={"model": self._settings.model, "input": list(texts)},
                headers=headers,
                timeout=self._settings.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingError("The embedding provider timed out.", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"The embedding provider could not be reached: {exc.__class__.__name__}.",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise _provider_error(response)

        return _vectors(response.json(), expected=len(texts), dimension=self.dimension)


def _provider_error(response: httpx.Response) -> EmbeddingError:
    retryable = response.status_code == 429 or response.status_code >= 500
    error = EmbeddingError(
        f"The embedding provider returned {response.status_code}: {_detail(response)}",
        retryable=retryable,
    )
    header = response.headers.get("retry-after")
    if header:
        # Seconds only. The HTTP-date form is legal and no provider uses it; parsing it
        # wrong would be worse than falling back to our own backoff.
        with contextlib.suppress(ValueError):
            error.retry_after = float(header)  # type: ignore[attr-defined]
    return error


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200].strip() or "no detail"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:200]
    return str(body)[:200]


def _vectors(body: Any, *, expected: int, dimension: int) -> list[list[float]]:
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or len(rows) != expected:
        raise EmbeddingError(
            "The embedding provider returned a response this build does not understand.",
        )
    # Sorted by the provider's own index rather than trusting array order: the API
    # documents that the results may come back out of order, and a silently transposed
    # pair of vectors is a retrieval bug nobody would ever trace back to here.
    ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
    vectors = [[float(value) for value in row["embedding"]] for row in ordered]
    for vector in vectors:
        if len(vector) != dimension:
            raise EmbeddingError(
                f"The embedding model returned {len(vector)}-dimension vectors but the "
                f"platform is configured for {dimension}. Reindex is required after a "
                "model change."
            )
    return vectors


def _batches(texts: Sequence[str], size: int) -> list[Sequence[str]]:
    batches: list[Sequence[str]] = []
    current: list[str] = []
    budget = 0
    for text in texts:
        if current and (len(current) >= size or budget + len(text) > MAX_BATCH_CHARS):
            batches.append(current)
            current, budget = [], 0
        current.append(text)
        budget += len(text)
    if current:
        batches.append(current)
    return batches


def _backoff(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(max(retry_after, 0.0), BACKOFF_CAP_SECONDS)
    # Full jitter. A document's batches all fail at the same instant, and a fixed backoff
    # would send them all back together.
    ceiling = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
    return random.uniform(0, ceiling)


# ---------------------------------------------------------------------------
# local
# ---------------------------------------------------------------------------

_WORDS = re.compile(r"\w+", re.UNICODE)


@dataclass
class HashEmbedder:
    """The hashing trick: a bag of words projected onto a fixed number of buckets.

    Deterministic, offline, and genuinely lexical — shared words produce a higher cosine
    similarity, which is enough to demonstrate the whole ingest-and-search path and enough
    to write assertions against. It is not semantic, and the log line on construction says
    so, because a deployment that reached production on this would look like it worked.
    """

    dimension: int = 256
    model: str = "hash-bow"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        buckets = [0.0] * self.dimension
        for word in _WORDS.findall(text.lower()):
            digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            # The sign bit spreads collisions instead of piling them up, which is the
            # whole reason the hashing trick works at these sizes.
            buckets[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in buckets))
        if norm == 0.0:
            # An empty or punctuation-only chunk. A zero vector has undefined cosine
            # similarity, so it is given one fixed direction instead: such chunks then
            # match each other and nothing else, which is the truthful outcome.
            buckets[0] = 1.0
            return buckets
        return [value / norm for value in buckets]


def build_embedder(settings: EmbeddingSettings, client: httpx.AsyncClient) -> Embedder:
    if settings.provider == "hash":
        logger.warning(
            "using the local hashing embedder; retrieval is lexical, not semantic",
            extra={"dimension": settings.dimension},
        )
        return HashEmbedder(dimension=settings.dimension, model=settings.model)
    return OpenAIEmbedder(client, settings)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "Embedder",
    "EmbeddingError",
    "EmbeddingSettings",
    "HashEmbedder",
    "OpenAIEmbedder",
    "build_embedder",
]
