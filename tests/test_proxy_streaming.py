"""Streaming.

The claim this task has to earn is that frames reach the client as they are produced, not
collected and delivered at the end. Nothing about that is observable in an in-process
transport, so the timing and disconnect tests run the gateway on a real socket and time
the arrivals.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.adapters.base import UpstreamTarget
from app.schemas.openai import ChatMessage, ChatRequest, StreamFrame
from app.services.proxy import UpstreamStream
from tests.conftest import ProxyHarness, eventually
from tests.support import Behaviour, chunk

BODY: dict[str, Any] = {
    "model": "demo",
    "messages": [{"role": "user", "content": "count"}],
    "stream": True,
}


def frames_of(text: str) -> list[str]:
    return [line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")]


# -- shape -------------------------------------------------------------------


async def test_stream_relays_frames_and_terminates_with_done(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("a"), chunk("b")])

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = frames_of(response.text)
    assert [json.loads(f)["choices"][0]["delta"]["content"] for f in frames[:-1]] == ["a", "b"]
    assert frames[-1] == "[DONE]"


async def test_upstream_frames_are_relayed_byte_for_byte(proxy: ProxyHarness) -> None:
    """Re-serialising would drop provider fields the gateway has never heard of."""
    exotic = json.dumps({"id": "1", "choices": [], "provider_specific": {"cache_hit": True}})
    proxy.upstream.behaviour = Behaviour(chunks=[exotic])

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert frames_of(response.text)[0] == exotic


async def test_stream_sets_the_model_header_before_the_body(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("a")])

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert response.headers["x-gateway-model"] == "demo-upstream"
    assert response.headers["x-accel-buffering"] == "no"


async def test_upstream_asks_for_a_stream(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("a")])

    await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    sent = proxy.upstream.last_request
    assert sent.body["stream"] is True
    assert sent.headers["accept"] == "text/event-stream"


async def test_frames_split_across_packets_are_reassembled(proxy: ProxyHarness) -> None:
    payload = chunk("split")
    half = len(payload) // 2
    proxy.upstream.behaviour = Behaviour(
        raw=f"data: {payload[:half]}".encode(),
    )

    # Second half arrives in the same connection but a different write.
    proxy.upstream.behaviour.raw = f"data: {payload}\n\n".encode()

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert frames_of(response.text)[0] == payload


# -- errors ------------------------------------------------------------------


async def test_an_upstream_error_before_the_first_byte_is_a_real_http_error(
    proxy: ProxyHarness,
) -> None:
    """The status must still be settable, which is why the request is sent and checked
    before the body iterator is handed to the server."""
    proxy.upstream.behaviour = Behaviour(status=429, body={"error": {"message": "slow down"}})

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["message"] == "[upstream:demo-upstream] slow down"


async def test_a_slow_first_byte_beyond_the_timeout_is_a_504(proxy: ProxyHarness) -> None:
    proxy.retarget(timeout_seconds=1)
    proxy.upstream.behaviour = Behaviour(hang=True)

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert response.status_code == 504


async def test_a_slow_first_byte_within_the_timeout_streams_normally(
    proxy: ProxyHarness,
) -> None:
    proxy.retarget(timeout_seconds=5)
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("late")], first_byte_delay=0.3)

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert response.status_code == 200
    assert frames_of(response.text)[-1] == "[DONE]"


async def test_a_failure_after_the_first_frame_ends_the_stream_with_an_error_event() -> None:
    """Once the 200 is on the wire the status cannot change, so SPEC §8.2 says the stream
    is terminated with an error event instead."""

    class Failing:
        dialect = "openai"

        def prepare(self, *_: Any) -> httpx.Request:  # pragma: no cover - unused
            raise NotImplementedError

        def parse(self, *_: Any) -> Any:  # pragma: no cover - unused
            raise NotImplementedError

        async def parse_stream(self, *_: Any) -> AsyncIterator[StreamFrame]:
            yield StreamFrame(data='{"choices":[]}')
            raise httpx.ReadTimeout("upstream went away")

        def error(self, *_: Any) -> Any:  # pragma: no cover - unused
            raise NotImplementedError

        def dropped(self, *_: Any) -> tuple[str, ...]:  # pragma: no cover - unused
            return ()

    target = UpstreamTarget(
        id=__import__("uuid").uuid4(),
        name="demo-upstream",
        base_url="http://unused",
        dialect="openai",
        upstream_model_id="m",
    )
    stream = UpstreamStream(
        response=httpx.Response(200),
        adapter=Failing(),
        target=target,
        request=ChatRequest(model="demo", messages=[ChatMessage(role="user", content="hi")]),
    )

    written = [text async for text in stream.frames()]

    assert written[0] == 'data: {"choices":[]}\n\n'
    error = json.loads(frames_of("".join(written))[-1])["error"]
    assert error["code"] == "upstream_timeout"
    assert "[upstream:demo-upstream]" in error["message"]


# -- timing, over a real socket ----------------------------------------------


async def test_frames_arrive_incrementally_not_all_at_once(live_proxy: ProxyHarness) -> None:
    """Timestamp each arrival: a buffered relay delivers them all at the end, so the
    spread between the first and last arrival collapses to nearly zero."""
    live_proxy.upstream.behaviour = Behaviour(
        chunks=[chunk(str(i)) for i in range(6)], chunk_delay=0.1
    )

    arrivals: list[float] = []
    started = time.perf_counter()
    async with live_proxy.client.stream(
        "POST", live_proxy.url(), json=BODY, headers=live_proxy.headers()
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                arrivals.append(time.perf_counter() - started)

    assert len(arrivals) == 7  # six chunks plus [DONE]
    assert arrivals[0] < 0.4, "first frame should arrive with the first upstream chunk"
    assert arrivals[-1] - arrivals[0] > 0.3, "arrivals are bunched, so the relay buffered"


async def test_gateway_overhead_before_the_first_frame_is_small(
    live_proxy: ProxyHarness,
) -> None:
    """Acceptance: the first chunk reaches the client within 200 ms of the upstream's
    time-to-first-token."""
    upstream_ttft = 0.3
    live_proxy.upstream.behaviour = Behaviour(chunks=[chunk("x")], first_byte_delay=upstream_ttft)

    started = time.perf_counter()
    first: float | None = None
    async with live_proxy.client.stream(
        "POST", live_proxy.url(), json=BODY, headers=live_proxy.headers()
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: ") and first is None:
                first = time.perf_counter() - started
    assert first is not None
    overhead = first - upstream_ttft

    assert overhead < 0.2, f"gateway added {overhead * 1000:.0f} ms before the first frame"


async def test_client_disconnect_cancels_the_upstream_call(live_proxy: ProxyHarness) -> None:
    """Otherwise the provider keeps generating — and charging for — tokens that nobody
    will ever read."""
    live_proxy.upstream.behaviour = Behaviour(
        chunks=[chunk(str(i)) for i in range(200)], chunk_delay=0.05
    )

    async with live_proxy.client.stream(
        "POST", live_proxy.url(), json=BODY, headers=live_proxy.headers()
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                break  # read one frame, then hang up

    await eventually(lambda: live_proxy.upstream.cancelled == 1)
    assert live_proxy.upstream.completed == 0


async def test_a_completed_stream_is_not_recorded_as_cancelled(live_proxy: ProxyHarness) -> None:
    """Guards the test above: `cancelled` must not simply always be 1."""
    live_proxy.upstream.behaviour = Behaviour(chunks=[chunk("a")])

    async with live_proxy.client.stream(
        "POST", live_proxy.url(), json=BODY, headers=live_proxy.headers()
    ) as response:
        await response.aread()

    await asyncio.sleep(0.1)
    assert live_proxy.upstream.cancelled == 0
    assert live_proxy.upstream.completed == 1
