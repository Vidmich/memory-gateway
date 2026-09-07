"""The acceptance criterion, stated literally: the official SDK works with only
``base_url`` changed.

Everything else in this suite checks the gateway's own view of the wire. This checks the
client's — that the SDK parses the bodies, raises its own typed exceptions for the error
statuses, and iterates the stream.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import openai
import pytest
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from tests.conftest import ProxyHarness
from tests.support import Behaviour, chunk, completion

MESSAGES: list[ChatCompletionMessageParam] = [{"role": "user", "content": "hi"}]


def sdk(proxy: ProxyHarness) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=f"{str(proxy.client.base_url).rstrip('/')}{proxy.url('')}",
        api_key=proxy.token,
        # The gateway's status codes are what is under test; the SDK retrying 429s and
        # 5xx would hide them behind a delay.
        max_retries=0,
    )


@pytest.fixture
async def client(live_proxy: ProxyHarness) -> AsyncIterator[AsyncOpenAI]:
    instance = sdk(live_proxy)
    try:
        yield instance
    finally:
        await instance.close()


async def test_completion(live_proxy: ProxyHarness, client: AsyncOpenAI) -> None:
    live_proxy.upstream.behaviour = Behaviour(body=completion("hello from the sdk"))

    result = await client.chat.completions.create(model="demo", messages=MESSAGES)

    assert result.choices[0].message.content == "hello from the sdk"
    assert result.usage is not None
    assert result.usage.total_tokens == 5


async def test_streaming(live_proxy: ProxyHarness, client: AsyncOpenAI) -> None:
    live_proxy.upstream.behaviour = Behaviour(chunks=[chunk("one "), chunk("two")])

    stream = await client.chat.completions.create(
        model="demo",
        messages=MESSAGES,
        stream=True,
    )
    text = "".join([part.choices[0].delta.content or "" async for part in stream])

    assert text == "one two"


async def test_models(live_proxy: ProxyHarness, client: AsyncOpenAI) -> None:
    listed = await client.models.list()

    assert [model.id for model in listed.data] == ["demo"]


async def test_a_bad_key_raises_authentication_error(client: AsyncOpenAI) -> None:
    client.api_key = "mg_deadbeef_nope"

    with pytest.raises(openai.AuthenticationError):
        await client.chat.completions.create(model="demo", messages=MESSAGES)


async def test_an_upstream_429_raises_rate_limit_error(
    live_proxy: ProxyHarness, client: AsyncOpenAI
) -> None:
    """The whole point of relaying the status: the SDK's own backoff logic depends on it."""
    live_proxy.upstream.behaviour = Behaviour(status=429, body={"error": {"message": "slow down"}})

    with pytest.raises(openai.RateLimitError):
        await client.chat.completions.create(model="demo", messages=MESSAGES)


async def test_an_unknown_model_raises_not_found(
    live_proxy: ProxyHarness, client: AsyncOpenAI
) -> None:
    with pytest.raises(openai.NotFoundError):
        await client.chat.completions.create(model="gpt-4o", messages=MESSAGES)


async def test_tools_raises_bad_request_naming_the_field(
    live_proxy: ProxyHarness, client: AsyncOpenAI
) -> None:
    with pytest.raises(openai.BadRequestError, match="tools"):
        await client.chat.completions.create(
            model="demo",
            messages=MESSAGES,
            tools=[{"type": "function", "function": {"name": "noop"}}],
        )


# ---------------------------------------------------------------------------
# against a real provider
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="no live provider key")
async def test_against_a_real_provider(live_proxy: ProxyHarness) -> None:
    """The one thing a mock cannot prove: that a real provider accepts what we send.

    Run with ``OPENAI_API_KEY=... uv run pytest -m live``. Costs a few tokens.
    """
    import dataclasses

    live_proxy.resolver.gateway = dataclasses.replace(
        live_proxy.gateway,
        targets=(
            dataclasses.replace(
                live_proxy.target,
                base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                upstream_model_id=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                credential=os.environ["OPENAI_API_KEY"],
                timeout_seconds=60,
            ),
        ),
    )
    client = sdk(live_proxy)

    result = await client.chat.completions.create(
        model="demo",
        messages=[{"role": "user", "content": "Reply with the single word: pong"}],
        max_tokens=5,
    )
    assert result.choices[0].message.content

    stream = await client.chat.completions.create(
        model="demo",
        messages=[{"role": "user", "content": "Count from 1 to 5."}],
        stream=True,
    )
    parts = [part async for part in stream]
    assert len(parts) > 1, "a real stream arrives in more than one chunk"

    await client.close()
