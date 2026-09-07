"""Test doubles for the data plane.

The upstream is a real ASGI app served over a real socket rather than a transport mock.
SSE behaviour — frames split across packets, a slow first byte, a client that hangs up
mid-stream — only exists at the socket level, and those are exactly the behaviours task 02
has to get right.
"""

from __future__ import annotations

import asyncio
import gc
import json
import uuid
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import uvicorn

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import GatewayNotFound
from app.core import keys
from app.core.ids import uuid7
from app.services.api_keys import AuthenticatedKey
from app.services.gateways import ResolvedGateway

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# mock upstream
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    path: str
    query: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class Behaviour:
    """What the next upstream call should do."""

    status: int = 200
    body: dict[str, Any] | None = None
    #: Raw SSE payloads, written one ``data:`` frame each.
    chunks: Sequence[str] = ()
    #: Written verbatim instead of ``chunks``, for testing frame splitting.
    raw: bytes | None = None
    first_byte_delay: float = 0.0
    chunk_delay: float = 0.0
    #: Accept the request and never answer, so the caller's read timeout fires.
    hang: bool = False
    send_done: bool = True


class MockUpstream:
    """A scriptable OpenAI-shaped provider."""

    def __init__(self) -> None:
        self.base_url = ""
        self.behaviour = Behaviour()
        self.requests: list[RecordedRequest] = []
        self.cancelled = 0
        self.completed = 0

    @property
    def last_request(self) -> RecordedRequest:
        return self.requests[-1]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - no lifespan for a bare app
            return

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        self.requests.append(
            RecordedRequest(
                path=scope["path"],
                query=scope["query_string"].decode(),
                headers={k.decode().lower(): v.decode() for k, v in scope["headers"]},
                body=json.loads(body) if body else {},
            )
        )

        behaviour = self.behaviour
        if behaviour.hang:
            # Accept the request and answer nothing, so the caller's read timeout fires.
            # Waiting on `receive` rather than sleeping means this ends the moment the
            # caller hangs up, instead of pinning the server open until shutdown.
            await receive()
            return

        if behaviour.chunks or behaviour.raw is not None:
            await self._stream(receive, send, behaviour)
        else:
            await self._respond(send, behaviour)

    async def _respond(self, send: Send, behaviour: Behaviour) -> None:
        payload = json.dumps(behaviour.body if behaviour.body is not None else {}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": behaviour.status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def _stream(self, receive: Receive, send: Send, behaviour: Behaviour) -> None:
        """Write frames, but stop the moment the client hangs up.

        Watching ``receive`` for ``http.disconnect`` alongside the writes is what a real
        provider does. Without it the server keeps writing into a half-closed socket —
        which succeeds — and the test could never tell a cancelled stream from a
        completed one.
        """
        await send(
            {
                "type": "http.response.start",
                "status": behaviour.status,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        writing = asyncio.create_task(self._write(send, behaviour))
        watching = asyncio.create_task(_wait_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait({writing, watching}, return_when=asyncio.FIRST_COMPLETED)
            if watching in done and writing not in done:
                self.cancelled += 1
            elif writing in done:
                writing.result()
                self.completed += 1
        finally:
            for task in (writing, watching):
                task.cancel()

    async def _write(self, send: Send, behaviour: Behaviour) -> None:
        try:
            if behaviour.first_byte_delay:
                await asyncio.sleep(behaviour.first_byte_delay)

            if behaviour.raw is not None:
                await send({"type": "http.response.body", "body": behaviour.raw, "more_body": True})
            else:
                for chunk in behaviour.chunks:
                    if behaviour.chunk_delay:
                        await asyncio.sleep(behaviour.chunk_delay)
                    await send(
                        {
                            "type": "http.response.body",
                            "body": f"data: {chunk}\n\n".encode(),
                            "more_body": True,
                        }
                    )
            if behaviour.send_done:
                await send(
                    {"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": True}
                )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:
            # `send` refusing because the peer is gone counts as a cancellation too.
            self.cancelled += 1
            raise


async def _wait_for_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


def completion(content: str = "hello", *, model: str = "upstream-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def chunk(content: str, *, finish_reason: str | None = None) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1_700_000_000,
            "model": "upstream-model",
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": finish_reason}
            ],
        }
    )


# ---------------------------------------------------------------------------
# fake resolver / authenticator
# ---------------------------------------------------------------------------


def make_target(base_url: str, **overrides: Any) -> UpstreamTarget:
    values: dict[str, Any] = {
        "id": uuid7(),
        "name": "demo-upstream",
        "base_url": base_url,
        "dialect": "openai",
        "upstream_model_id": "upstream-model",
        "auth_type": "bearer",
        "credential": "sk-upstream-secret",
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return UpstreamTarget(**values)


def make_gateway(target: UpstreamTarget, **overrides: Any) -> ResolvedGateway:
    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": uuid7(),
        "slug": "demo",
        "name": "Demo Gateway",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "targets": (target,),
    }
    values.update(overrides)
    return ResolvedGateway(**values)


@dataclass
class FakeResolver:
    gateway: ResolvedGateway
    error: Exception | None = None

    async def resolve(self, slug: str) -> ResolvedGateway:
        if self.error is not None:
            raise self.error
        if slug != self.gateway.slug:
            raise GatewayNotFound(f"No gateway with slug '{slug}'.")
        return self.gateway


@dataclass
class FakeAuthenticator:
    """Verifies against a small in-memory key table, using the real hashing code."""

    records: dict[uuid.UUID, tuple[str, uuid.UUID, datetime | None]] = field(default_factory=dict)
    touched: list[uuid.UUID] = field(default_factory=list)

    def issue(self, gateway_id: uuid.UUID, *, revoked: bool = False) -> str:
        minted = keys.mint(uuid7())
        self.records[minted.key_id] = (
            minted.key_hash,
            gateway_id,
            datetime.now(UTC) if revoked else None,
        )
        return minted.token

    async def authenticate(self, token: str | None) -> AuthenticatedKey:
        from app.api.proxy.errors import AuthenticationFailed

        parsed = keys.parse(token) if token else None
        record = self.records.get(parsed.key_id) if parsed is not None else None
        if parsed is None or record is None or not keys.verify(parsed.secret, record[0]):
            raise AuthenticationFailed("Incorrect API key provided.")
        if record[2] is not None:
            raise AuthenticationFailed("This API key has been revoked.")
        self.touched.append(parsed.key_id)
        return AuthenticatedKey(
            id=parsed.key_id,
            gateway_id=record[1],
            name="test",
            prefix=keys.display_prefix(parsed.key_id),
        )


# ---------------------------------------------------------------------------
# running an app on a real port
# ---------------------------------------------------------------------------


@asynccontextmanager
async def serve(app: Any, *, lifespan: Literal["auto", "on", "off"] = "off") -> AsyncIterator[str]:
    """Run an ASGI app on an ephemeral port and yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan=lifespan)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    # uvicorn exposes readiness as a plain flag rather than an event, so polling it is
    # the only option; the loop also breaks if startup failed, to surface that error.
    while not server.started and not task.done():  # noqa: ASYNC110
        await asyncio.sleep(0.005)
    if task.done():  # pragma: no cover - surfaces a startup failure
        task.result()

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await _stop(server, task)


async def _stop(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    """Shut a test server down without waiting on connections nobody will finish.

    Uvicorn's graceful shutdown awaits ``asyncio.Server.wait_closed()``, which does not
    return until every accepted connection has been torn down. A client that walks away
    mid-stream leaves one in a closing state that the Windows proactor loop never
    finishes, so the graceful path can hang forever on exactly the case the streaming
    tests exist to exercise. Try it politely, then take the sockets away.
    """
    server.should_exit = True
    deadline = asyncio.get_running_loop().time() + 3
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        for connection in list(getattr(server.server_state, "connections", ())):
            connection.shutdown()
        await asyncio.wait({task}, timeout=0.05)

    if task.done():
        await task
        return

    server.force_exit = True
    for listener in server.servers:
        listener.close()
    for connection in list(getattr(server.server_state, "connections", ())):
        transport = getattr(connection, "transport", None)
        if transport is not None:
            transport.abort()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    # Collect the abandoned connection's socket here, where its ResourceWarning can be
    # suppressed deliberately, rather than letting the garbage collector surface it in
    # the middle of an unrelated later test.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()
