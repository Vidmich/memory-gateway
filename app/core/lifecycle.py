"""Shutting down without truncating somebody's completion.

The failure this exists to prevent is specific and it happens on every deploy until it is
handled. Kubernetes removes a pod from its Service and sends ``SIGTERM`` at the same
moment, and neither the removal nor its propagation to every kube-proxy is instant. A
server that stops accepting the moment it is signalled therefore refuses requests that
were routed to it a few milliseconds earlier — a connection reset for the client, and
nothing in the pod's logs to explain it.

So ``SIGTERM`` starts a *drain* rather than a shutdown:

1. ``draining`` is set immediately, and ``/readyz`` starts answering 503. That is the
   signal the load balancer is actually watching, and it is the fastest thing this process
   can do.
2. New requests keep being served for ``drain_seconds`` — the window in which endpoints
   propagate. Requests still arriving here are ones somebody already decided to send us.
3. Only then is the server's own handler called, which stops accepting and waits for
   in-flight requests — a streaming completion included — to finish on their own.

``/healthz`` stays healthy throughout. A draining pod is not an unhealthy one, and a
liveness probe that failed here would get it killed mid-stream by the very mechanism
meant to protect it.

The one number that has to be right is in the chart, not here:
``terminationGracePeriodSeconds`` must exceed ``drain_seconds`` plus the longest upstream
call, or the kill arrives while step 3 is still doing its job.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import Callable
from contextlib import suppress
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)

#: Signals a container runtime sends to ask for a shutdown. ``SIGINT`` is here for the
#: developer pressing Ctrl-C, who gets the same drain — and finds out about a slow one
#: at their keyboard rather than during a deploy.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

Handler = Callable[[int, FrameType | None], Any] | int | None


class Lifecycle:
    """The process's readiness state, and the drain that flips it.

    Held on ``app.state`` and read by ``/readyz``. Deliberately a plain object with a
    boolean rather than an event: the readiness probe is a synchronous question asked from
    another task, and the answer has to be available the instant the signal lands.
    """

    def __init__(self, *, drain_seconds: float = 0.0) -> None:
        self.drain_seconds = max(0.0, drain_seconds)
        self._draining = False
        self._previous: dict[int, Handler] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def draining(self) -> bool:
        return self._draining

    # -- installation --------------------------------------------------------

    def install(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        """Take over the shutdown signals, remembering who had them.

        Returns whether anything was installed. Signal handlers can only be set from the
        main thread, and a test client or an embedded server may be neither in it nor
        alone in the process — in which case the drain is simply not armed and the server
        shuts down the way it did before this module existed.
        """
        if threading.current_thread() is not threading.main_thread():
            return False
        # The loop is only needed to schedule the delayed hand-off. Installed outside one —
        # a synchronous entry point, a test — the drain still flips readiness and simply
        # chains straight through, which is the behaviour a process with no server has.
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        for number in SHUTDOWN_SIGNALS:
            try:
                self._previous[number] = signal.signal(number, self._on_signal)
            except (ValueError, OSError):  # pragma: no cover - platform dependent
                logger.debug("could not install a handler for signal %s", number)
        return bool(self._previous)

    def restore(self) -> None:
        """Give the signals back. Called on the way out so a test that built an app does
        not leave this object holding the process's handlers."""
        for number, handler in self._previous.items():
            with suppress(ValueError, OSError):  # pragma: no cover - platform dependent
                signal.signal(number, handler)
        self._previous.clear()

    # -- the drain -----------------------------------------------------------

    def _on_signal(self, number: int, frame: FrameType | None) -> None:
        """Runs inside the signal handler, so it does as little as possible.

        A second signal means somebody is impatient, or the orchestrator has escalated.
        Either way the drain is over and the previous handler runs at once — waiting out a
        grace period nobody is waiting for is how a shutdown becomes a ``SIGKILL``.
        """
        if self._draining:
            self._chain(number, frame)
            return

        self._draining = True
        logger.info(
            "draining: readiness is now failing, still serving in-flight work",
            extra={"signal": number, "drain_seconds": self.drain_seconds},
        )
        if self.drain_seconds <= 0 or self._loop is None or not self._loop.is_running():
            self._chain(number, frame)
            return
        # `call_soon_threadsafe` because a signal handler interrupts whatever the main
        # thread was doing, which may be inside the loop's own machinery.
        self._loop.call_soon_threadsafe(
            self._loop.call_later, self.drain_seconds, self._chain, number, frame
        )

    def _chain(self, number: int, frame: FrameType | None) -> None:
        """Hand the signal to whoever had it before us — normally uvicorn's handler, which
        stops accepting new connections and waits for the in-flight ones."""
        handler = self._previous.get(number)
        if callable(handler):
            handler(number, frame)
        elif handler == signal.SIG_DFL:  # pragma: no cover - not how servers install them
            signal.signal(number, signal.SIG_DFL)
            signal.raise_signal(number)

    # -- for tests and for a shutdown that did not come from a signal ---------

    def begin_drain(self) -> None:
        self._draining = True


__all__ = ["SHUTDOWN_SIGNALS", "Lifecycle"]
