"""Draining on SIGTERM (task 18).

The failure being prevented is a connection reset for a customer whose request was routed
here microseconds before the pod was removed from its Service. Nothing about it is visible
in a normal test run — it needs a deploy under load to show up — so what is asserted here
is the mechanism: readiness fails first, the server keeps serving for a window, and the
signal reaches the server's own handler afterwards rather than instead.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.lifecycle import SHUTDOWN_SIGNALS, Lifecycle
from app.main import create_app


class Server:
    """Stands in for uvicorn's handler: the thing that stops accepting connections."""

    def __init__(self) -> None:
        self.stopped: list[int] = []

    def __call__(self, number: int, frame: object) -> None:
        self.stopped.append(number)


@pytest.fixture
def server() -> Iterator[Server]:
    """Installs a fake server handler and puts the real ones back afterwards, so a test
    cannot leave this process unable to be stopped."""
    handler = Server()
    previous = {number: signal.getsignal(number) for number in SHUTDOWN_SIGNALS}
    for number in SHUTDOWN_SIGNALS:
        signal.signal(number, handler)
    try:
        yield handler
    finally:
        for number, original in previous.items():
            signal.signal(number, original)


# ---------------------------------------------------------------------------
# the drain itself
# ---------------------------------------------------------------------------


def test_readiness_fails_the_instant_the_signal_lands(server: Server) -> None:
    """Step one, and the only one with no delay in it. Everything else about the shutdown
    is waiting; this is the part that has to be immediate, because it is what the load
    balancer is watching."""
    lifecycle = Lifecycle(drain_seconds=0)
    lifecycle.install()
    try:
        assert not lifecycle.draining

        lifecycle._on_signal(signal.SIGTERM, None)

        assert lifecycle.draining
    finally:
        lifecycle.restore()


def test_the_signal_reaches_the_server_rather_than_being_swallowed(server: Server) -> None:
    """With no drain window there is nothing to wait for, so the handler runs at once.

    Worth its own case: a drain that forgot to chain would leave a pod that fails readiness
    for ever and never exits, which the orchestrator eventually resolves with a SIGKILL —
    the exact outcome this module exists to avoid, arrived at from the other direction.
    """
    lifecycle = Lifecycle(drain_seconds=0)
    lifecycle.install()
    try:
        lifecycle._on_signal(signal.SIGTERM, None)
    finally:
        lifecycle.restore()

    assert server.stopped == [signal.SIGTERM]


async def test_the_server_is_stopped_only_after_the_drain_window(server: Server) -> None:
    lifecycle = Lifecycle(drain_seconds=0.15)
    lifecycle.install(asyncio.get_running_loop())
    try:
        lifecycle._on_signal(signal.SIGTERM, None)

        # Readiness has already flipped; the server has not been touched.
        assert lifecycle.draining
        assert server.stopped == []

        await asyncio.sleep(0.05)
        assert server.stopped == [], "still inside the window"

        await asyncio.sleep(0.3)
        assert server.stopped == [signal.SIGTERM]
    finally:
        lifecycle.restore()


async def test_a_second_signal_ends_the_wait_immediately(server: Server) -> None:
    """Somebody pressed Ctrl-C twice, or the orchestrator escalated. Waiting out a grace
    period nobody is waiting for is how a shutdown becomes a SIGKILL."""
    lifecycle = Lifecycle(drain_seconds=30)
    lifecycle.install(asyncio.get_running_loop())
    try:
        lifecycle._on_signal(signal.SIGINT, None)
        assert server.stopped == []

        lifecycle._on_signal(signal.SIGINT, None)

        assert server.stopped == [signal.SIGINT]
    finally:
        lifecycle.restore()


def test_restoring_gives_the_signals_back(server: Server) -> None:
    lifecycle = Lifecycle()
    lifecycle.install()
    assert signal.getsignal(signal.SIGTERM) is not server

    lifecycle.restore()

    assert signal.getsignal(signal.SIGTERM) is server


def test_a_negative_drain_is_treated_as_none() -> None:
    assert Lifecycle(drain_seconds=-5).drain_seconds == 0.0


# ---------------------------------------------------------------------------
# what the probes say while it is happening
# ---------------------------------------------------------------------------


async def test_readyz_answers_503_while_draining(app: FastAPI, client: AsyncClient) -> None:
    lifecycle: Lifecycle = app.state.lifecycle
    lifecycle.begin_drain()

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "draining"}


async def test_healthz_stays_healthy_while_draining(app: FastAPI, client: AsyncClient) -> None:
    """A draining pod is not an unhealthy one. A liveness probe that failed here would get
    it killed mid-stream by the very mechanism meant to protect it."""
    app.state.lifecycle.begin_drain()

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_a_draining_readiness_check_probes_nothing(app: FastAPI, client: AsyncClient) -> None:
    """The answer does not depend on Postgres being up, and a shutdown is not the moment to
    add four network calls to a probe that runs every couple of seconds. That it returns at
    all with no backing services reachable is the assertion."""
    app.state.lifecycle.begin_drain()

    response = await client.get("/readyz")

    assert set(response.json()) == {"status"}


async def test_the_drain_window_comes_from_configuration() -> None:
    settings = get_settings().model_copy(update={"shutdown_drain_seconds": 7.5})
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        assert application.state.lifecycle.drain_seconds == 7.5


async def test_an_app_that_never_started_a_server_still_shuts_down_cleanly() -> None:
    """The lifespan installs handlers and gives them back. A test suite that builds a few
    hundred apps must not end up with one of them holding the process's SIGTERM."""
    before = signal.getsignal(signal.SIGTERM)

    application = create_app()
    async with application.router.lifespan_context(application):
        pass
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver"):
        pass

    assert signal.getsignal(signal.SIGTERM) is before
