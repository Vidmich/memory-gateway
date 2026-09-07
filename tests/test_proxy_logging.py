"""What the proxy records, driven through the real route over real HTTP.

Everything here goes through the app: routing, authentication, the adapter, a scriptable
provider on a real socket. Only the log *writer* is memory, so the assertions are about
what the request path collected rather than about what a mock was told.

The three acceptance criteria this module owns:

* a streamed response reassembles into the same transcript the non-streamed equivalent
  stores, for the same prompt;
* switching off a body toggle omits exactly that field and nothing else;
* the request path adds no synchronous database call — asserted by counting, not assumed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from app.services.request_log import LogPolicy
from tests.conftest import ProxyHarness, eventually
from tests.support import Behaviour, chunk, completion

PROMPT = {"model": "demo", "messages": [{"role": "user", "content": "what is the answer"}]}


def with_policy(
    proxy: ProxyHarness,
    *,
    request_body: bool = True,
    assembled_prompt: bool = True,
    response_body: bool = True,
    redaction_patterns: tuple[str, ...] = (),
) -> None:
    """Re-resolve the gateway with a different logging policy."""
    proxy.resolver.gateway = replace(
        proxy.gateway,
        log_policy=LogPolicy(
            request_body=request_body,
            assembled_prompt=assembled_prompt,
            response_body=response_body,
            redaction_patterns=redaction_patterns,
        ),
    )


async def send(proxy: ProxyHarness, body: dict[str, Any] | None = None) -> Any:
    return await proxy.client.post(proxy.url(), json=body or PROMPT, headers=proxy.headers())


async def row(proxy: ProxyHarness) -> Any:
    await proxy.logs.flush()
    rows = proxy.logs.rows
    assert len(rows) == 1, f"expected one log row, got {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


async def test_a_completion_is_recorded(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion("42"))

    response = await send(proxy)
    logged = await row(proxy)

    assert response.status_code == 200
    assert logged.status_code == 200
    assert logged.organization_id == proxy.gateway.organization_id
    assert logged.gateway_id == proxy.gateway.id
    assert logged.model_name == proxy.target.name
    assert logged.streamed is False
    assert logged.latency_total_ms >= 0


async def test_the_row_names_the_key_that_made_the_call(proxy: ProxyHarness) -> None:
    """``api_keys`` rows survive revocation precisely so this reference resolves."""
    proxy.upstream.behaviour = Behaviour(body=completion())

    await send(proxy)
    logged = await row(proxy)

    assert logged.api_key_id == proxy.authenticator.touched[-1]


async def test_the_row_carries_the_request_id_the_client_was_given(
    proxy: ProxyHarness,
) -> None:
    """A support ticket quoting the header has to lead to this row."""
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await send(proxy)
    logged = await row(proxy)

    assert logged.request_id == response.headers["x-gateway-request-id"]


async def test_token_counts_come_from_the_provider(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    await send(proxy)
    logged = await row(proxy)

    assert (logged.prompt_tokens, logged.completion_tokens) == (3, 2)


async def test_the_transcript_holds_the_client_request_and_the_assembled_prompt(
    proxy: ProxyHarness,
) -> None:
    """The whole point of the detail view: what the caller sent, and what actually went
    upstream, side by side."""
    proxy.resolver.gateway = replace(proxy.gateway, system_context="Be concise.")
    proxy.upstream.behaviour = Behaviour(body=completion("42"))

    await send(proxy)
    logged = await row(proxy)
    transcript = proxy.logs.transcript(logged.id)

    assert transcript is not None
    assert transcript.request_body == [{"role": "user", "content": "what is the answer"}]
    assert transcript.assembled_prompt is not None
    assert transcript.assembled_prompt[0]["role"] == "system"
    assert "Be concise." in transcript.assembled_prompt[0]["content"]
    assert transcript.response_body == "42"


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_a_streamed_response_reassembles_into_the_same_transcript(
    proxy: ProxyHarness,
) -> None:
    """The acceptance criterion, end to end.

    The same generation is sent once as a completion and once as deltas, and the two
    stored transcripts have to be the same string — not merely similar.
    """
    proxy.upstream.behaviour = Behaviour(body=completion("Hello, world"))
    await send(proxy)
    await proxy.logs.flush()
    non_streamed = proxy.logs.transcript(proxy.logs.rows[0].id)

    proxy.logs.database.request_logs.clear()
    proxy.logs.database.transcripts.clear()
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("Hello, "), chunk("world")])
    async with proxy.client.stream(
        "POST", proxy.url(), json={**PROMPT, "stream": True}, headers=proxy.headers()
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_bytes():
            pass

    await eventually(lambda: bool(proxy.logs.queue.qsize()))
    streamed = proxy.logs.transcript((await row(proxy)).id)

    assert non_streamed is not None
    assert streamed is not None
    assert streamed.response_body == non_streamed.response_body == "Hello, world"


async def test_a_streamed_row_records_the_first_token_time(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("hi")])

    async with proxy.client.stream(
        "POST", proxy.url(), json={**PROMPT, "stream": True}, headers=proxy.headers()
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    await eventually(lambda: bool(proxy.logs.queue.qsize()))
    logged = await row(proxy)

    assert logged.streamed is True
    assert logged.latency_ttft_ms is not None


async def test_a_stream_that_fails_mid_flight_is_still_recorded(
    proxy: ProxyHarness,
) -> None:
    """The status line said 200 and it was true — the client got a partial response.

    Recording the status honestly and the cause separately keeps the error taxonomy from
    claiming a 200 failed, while still leaving the failure findable.
    """
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("half")], send_done=False)

    async with proxy.client.stream(
        "POST", proxy.url(), json={**PROMPT, "stream": True}, headers=proxy.headers()
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    await eventually(lambda: bool(proxy.logs.queue.qsize()))
    logged = await row(proxy)

    assert logged.status_code == 200
    assert proxy.logs.transcript(logged.id) is not None


# ---------------------------------------------------------------------------
# the toggles
# ---------------------------------------------------------------------------


async def test_switching_off_the_response_body_omits_exactly_that(
    proxy: ProxyHarness,
) -> None:
    with_policy(proxy, response_body=False)
    proxy.upstream.behaviour = Behaviour(body=completion("secret answer"))

    await send(proxy)
    logged = await row(proxy)
    transcript = proxy.logs.transcript(logged.id)

    assert transcript is not None
    assert transcript.response_body is None
    # And nothing else moved.
    assert transcript.request_body is not None
    assert transcript.assembled_prompt is not None
    assert logged.completion_tokens == 2


async def test_switching_off_the_request_body_omits_exactly_that(
    proxy: ProxyHarness,
) -> None:
    with_policy(proxy, request_body=False)
    proxy.upstream.behaviour = Behaviour(body=completion())

    await send(proxy)
    transcript = proxy.logs.transcript((await row(proxy)).id)

    assert transcript is not None
    assert transcript.request_body is None
    assert transcript.assembled_prompt is not None
    assert transcript.response_body is not None


async def test_switching_off_the_assembled_prompt_omits_exactly_that(
    proxy: ProxyHarness,
) -> None:
    with_policy(proxy, assembled_prompt=False)
    proxy.upstream.behaviour = Behaviour(body=completion())

    await send(proxy)
    transcript = proxy.logs.transcript((await row(proxy)).id)

    assert transcript is not None
    assert transcript.assembled_prompt is None
    assert transcript.request_body is not None


async def test_switching_everything_off_still_records_metadata(
    proxy: ProxyHarness,
) -> None:
    """``log_metadata`` is forced true in the schema: the monitoring charts are the part
    an operator cannot run the product without."""
    with_policy(proxy, request_body=False, assembled_prompt=False, response_body=False)
    proxy.upstream.behaviour = Behaviour(body=completion())

    await send(proxy)
    logged = await row(proxy)

    assert proxy.logs.transcript(logged.id) is None
    assert logged.status_code == 200
    assert logged.latency_total_ms >= 0
    assert logged.bodies_omitted is None, "nothing dropped these; the gateway asked for it"


async def test_a_stream_with_capture_off_copies_nothing(proxy: ProxyHarness) -> None:
    """The cheapest possible "off": no bytes are copied out of the stream at all."""
    with_policy(proxy, response_body=False)
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("hello")])

    async with proxy.client.stream(
        "POST", proxy.url(), json={**PROMPT, "stream": True}, headers=proxy.headers()
    ) as response:
        async for _ in response.aiter_bytes():
            pass

    await eventually(lambda: bool(proxy.logs.queue.qsize()))
    logged = await row(proxy)
    transcript = proxy.logs.transcript(logged.id)

    assert transcript is None or transcript.response_body is None


async def test_redaction_applies_to_what_the_proxy_recorded(proxy: ProxyHarness) -> None:
    """Before persistence, over the real request path."""
    with_policy(proxy, redaction_patterns=(r"[\w.+-]+@[\w-]+\.[\w.]+",))
    proxy.upstream.behaviour = Behaviour(body=completion("write to ada@example.com"))

    await send(proxy, {**PROMPT, "messages": [{"role": "user", "content": "ada@example.com"}]})
    transcript = proxy.logs.transcript((await row(proxy)).id)

    assert transcript is not None
    assert "ada@example.com" not in json.dumps(transcript.request_body)
    assert "ada@example.com" not in (transcript.response_body or "")


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------


async def test_an_upstream_failure_is_recorded_with_its_code(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(
        status=429, body={"error": {"message": "slow down", "type": "rate_limit_error"}}
    )

    response = await send(proxy)
    logged = await row(proxy)

    assert response.status_code == 429
    assert logged.status_code == 429
    assert logged.error_code is not None


async def test_a_client_error_after_authorization_is_recorded(proxy: ProxyHarness) -> None:
    """A 400 belongs to the organization whose endpoint produced it, and the request log
    is where they will come looking for it."""
    response = await send(proxy, {**PROMPT, "model": "not-this-gateway"})
    logged = await row(proxy)

    assert response.status_code == 404
    assert logged.status_code == 404
    assert logged.error_code == "model_not_found"


async def test_an_unparseable_body_is_recorded(proxy: ProxyHarness) -> None:
    response = await proxy.client.post(
        proxy.url(),
        content=b"{not json",
        headers={**proxy.headers(), "content-type": "application/json"},
    )
    logged = await row(proxy)

    assert response.status_code == 400
    assert logged.status_code == 400


async def test_an_authentication_failure_is_not_recorded(proxy: ProxyHarness) -> None:
    """It belongs to no organization, so there is no monitoring screen it could honestly
    appear on. It stays in the access log and in Prometheus."""
    response = await proxy.client.post(
        proxy.url(), json=PROMPT, headers={"Authorization": "Bearer mg_nonsense"}
    )
    await proxy.logs.flush()

    assert response.status_code == 401
    assert proxy.logs.rows == []


# ---------------------------------------------------------------------------
# the latency promise
# ---------------------------------------------------------------------------


async def test_the_request_path_makes_no_database_call(proxy: ProxyHarness) -> None:
    """The acceptance criterion "logging adds under 5 ms" rests on this, and this is the
    part that can be asserted rather than measured: the writer is not called at all until
    something asks it to flush.

    A timing assertion would be the obvious alternative and it would be worthless — it
    would pass on an unloaded laptop with a synchronous insert in the request path, which
    is exactly the mistake it is supposed to catch.
    """
    written: list[int] = []
    original = proxy.logs.writer.write

    async def counting(records: Any) -> None:
        written.append(len(records))
        await original(records)

    proxy.logs.writer.write = counting  # type: ignore[method-assign]
    proxy.upstream.behaviour = Behaviour(body=completion())

    for _ in range(5):
        await send(proxy)

    assert written == [], "the request path wrote to the log store synchronously"

    await proxy.logs.flush()

    assert written == [5], "five requests should flush as one batch, not five"


@pytest.mark.parametrize("count", [3])
async def test_many_requests_batch_into_one_write(proxy: ProxyHarness, count: int) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    for _ in range(count):
        await send(proxy)
    flushed = await proxy.logs.flush()

    assert flushed == count
    assert len(proxy.logs.rows) == count
