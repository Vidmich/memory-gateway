"""Embedding the chunks: batching, retries, and the local fallback.

The provider is driven through a real ``httpx`` transport rather than a stubbed client, so
the request that goes out — its URL, its body, its authorization header — is the request a
provider would actually receive.

The sharpest test here is :func:`test_vectors_are_paired_with_their_inputs_by_index`. The
OpenAI API documents that results may come back out of order; an implementation that
trusted the array order would pass every other test in this file and silently transpose
two chunks' vectors in production, which is a retrieval bug nobody would trace back here.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.embeddings import (
    EmbeddingError,
    EmbeddingSettings,
    HashEmbedder,
    OpenAIEmbedder,
    build_embedder,
)

SETTINGS = EmbeddingSettings(
    provider="openai",
    model="text-embedding-3-small",
    dimension=4,
    base_url="https://api.example.com/v1",
    api_key="sk-test",
    batch_size=2,
)


def vectors(count: int, *, dimension: int = 4) -> list[dict[str, Any]]:
    return [
        {"index": index, "embedding": [float(index)] + [0.0] * (dimension - 1)}
        for index in range(count)
    ]


def embedder(
    handler: Any, *, settings: EmbeddingSettings = SETTINGS, **kwargs: Any
) -> OpenAIEmbedder:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIEmbedder(client, settings, sleep=_no_wait, **kwargs)


async def _no_wait(_: float) -> None:
    return None


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------


async def test_the_request_goes_to_the_embeddings_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": vectors(1)})

    await embedder(handler).embed(["hello"])

    assert str(seen[0].url) == "https://api.example.com/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer sk-test"


async def test_a_provider_with_no_key_sends_no_authorization_header() -> None:
    """A self-hosted TEI or Ollama endpoint. Sending ``Bearer None`` would be rejected."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": vectors(1)})

    settings = EmbeddingSettings(
        provider="openai", model="m", dimension=4, base_url="http://local", api_key=None
    )
    await embedder(handler, settings=settings).embed(["hello"])

    assert "authorization" not in seen[0].headers


async def test_nothing_is_sent_for_an_empty_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not have called the provider")

    assert await embedder(handler).embed([]) == []


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------


async def test_inputs_are_batched() -> None:
    """One request per chunk turns a 400-chunk document into 400 round trips."""
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        sizes.append(len(body["input"]))
        return httpx.Response(200, json={"data": vectors(len(body["input"]))})

    result = await embedder(handler).embed(["a", "b", "c", "d", "e"])

    assert sizes == [2, 2, 1]
    assert len(result) == 5


async def test_a_huge_input_is_split_by_size_as_well_as_by_count() -> None:
    """A provider rejects an oversized batch with a 400, and a 400 is not retryable."""
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        sizes.append(len(body["input"]))
        return httpx.Response(200, json={"data": vectors(len(body["input"]))})

    settings = EmbeddingSettings(
        provider="openai", model="m", dimension=4, base_url="http://x", batch_size=100
    )
    await embedder(handler, settings=settings).embed(["x" * 150_000, "y" * 150_000])

    assert sizes == [1, 1]


async def test_the_results_come_back_in_the_order_they_went_out() -> None:
    """The caller pairs them with chunk indexes positionally."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(ord(text[0])), 0.0, 0.0, 0.0]}
                    for index, text in enumerate(body["input"])
                ]
            },
        )

    result = await embedder(handler).embed(["a", "b", "c"])

    assert [row[0] for row in result] == [97.0, 98.0, 99.0]


async def test_vectors_are_paired_with_their_inputs_by_index() -> None:
    """The API documents that results may come back out of order. Trusting the array
    would transpose two chunks' vectors, which is a retrieval bug nobody would trace to
    this line."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2.0, 0.0, 0.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0, 0.0]},
                ]
            },
        )

    result = await embedder(handler).embed(["first", "second"])

    assert [row[0] for row in result] == [1.0, 2.0]


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


async def test_a_429_is_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json={"data": vectors(1)})

    result = await embedder(handler).embed(["hello"])

    assert len(calls) == 3
    assert len(result) == 1


async def test_the_retry_after_header_is_honoured() -> None:
    """It is the provider telling us exactly how long to wait. Ignoring it in favour of
    our own backoff is how a throttled client stays throttled."""
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        if not waits:
            return httpx.Response(429, headers={"retry-after": "7"}, json={})
        return httpx.Response(200, json={"data": vectors(1)})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await OpenAIEmbedder(client, SETTINGS, sleep=record).embed(["hello"])

    assert waits == [7.0]


async def test_a_400_is_not_retried() -> None:
    """The batch is malformed and will be malformed again. Four attempts spend four
    workers to reach the same answer."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "input too long"}})

    with pytest.raises(EmbeddingError) as error:
        await embedder(handler).embed(["hello"])

    assert len(calls) == 1
    assert error.value.retryable is False
    assert "input too long" in str(error.value)


async def test_a_persistent_5xx_gives_up_after_the_attempt_ceiling() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="upstream unavailable")

    with pytest.raises(EmbeddingError):
        await embedder(handler, max_attempts=3).embed(["hello"])

    assert len(calls) == 3


async def test_a_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(EmbeddingError) as error:
        await embedder(handler, max_attempts=1).embed(["hello"])

    assert error.value.retryable is True


async def test_a_wrong_dimension_is_reported_rather_than_indexed() -> None:
    """A collection's vectors must all come from one model. Accepting a mismatch here
    would fail deep inside Qdrant with a message about nothing recognisable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    with pytest.raises(EmbeddingError, match="Reindex"):
        await embedder(handler).embed(["hello"])


async def test_a_response_with_the_wrong_number_of_vectors_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": vectors(1)})

    with pytest.raises(EmbeddingError):
        await embedder(handler).embed(["one", "two"])


async def test_a_response_that_is_not_the_expected_shape_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(EmbeddingError, match="does not understand"):
        await embedder(handler).embed(["hello"])


# ---------------------------------------------------------------------------
# the local embedder
# ---------------------------------------------------------------------------


async def test_the_hash_embedder_is_deterministic() -> None:
    first = await HashEmbedder(dimension=32).embed(["hello world"])
    second = await HashEmbedder(dimension=32).embed(["hello world"])

    assert first == second


async def test_the_hash_embedder_produces_the_configured_dimension() -> None:
    [vector] = await HashEmbedder(dimension=17).embed(["hello"])

    assert len(vector) == 17


async def test_shared_words_score_higher_than_unrelated_ones() -> None:
    """Lexical, not semantic — but genuinely lexical, which is what makes the whole
    ingest-and-search path demonstrable without a provider key."""
    import math

    model = HashEmbedder(dimension=256)
    query = model.vector("annual leave policy")
    close = model.vector("the annual leave policy is twenty-five days")
    far = model.vector("kubernetes ingress controller configuration")

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(query, close) > cosine(query, far)
    assert math.isclose(sum(value * value for value in query), 1.0, rel_tol=1e-6)


async def test_an_empty_chunk_gets_a_direction_rather_than_a_zero_vector() -> None:
    """A zero vector has undefined cosine similarity. Giving empty chunks one fixed
    direction makes them match each other and nothing else, which is truthful."""
    [vector] = await HashEmbedder(dimension=8).embed(["   "])

    assert sum(abs(value) for value in vector) > 0


def test_the_provider_is_chosen_by_configuration() -> None:
    client = httpx.AsyncClient()
    local = build_embedder(
        EmbeddingSettings(provider="hash", model="hash-bow", dimension=64), client
    )
    remote = build_embedder(SETTINGS, client)

    assert isinstance(local, HashEmbedder)
    assert isinstance(remote, OpenAIEmbedder)
    assert local.dimension == 64
    assert remote.model == "text-embedding-3-small"


def test_the_provider_key_never_appears_in_a_repr() -> None:
    """A dataclass repr is what ends up in a traceback, a log line, or a debugger session,
    and SPEC §5.4 carves out no exception for any of them."""
    rendered = repr(SETTINGS)

    assert "sk-test" not in rendered
    assert "api_key='set'" in rendered
    assert "text-embedding-3-small" in rendered
