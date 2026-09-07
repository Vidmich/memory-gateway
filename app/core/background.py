"""Fire-and-forget work.

``asyncio.create_task`` alone is a bug: the event loop keeps only a weak reference, so a
task can be garbage-collected mid-flight, and an exception inside one is never raised
anywhere. This keeps a strong reference until completion and logs whatever went wrong.

For anything that must actually happen, use the queue (task 09) instead — this is for work
whose failure is acceptable, like stamping ``last_used_at``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_running: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    task = asyncio.create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task[Any]) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "background task failed",
            extra={"task": task.get_name()},
            exc_info=error,
        )


async def drain(timeout_seconds: float = 5.0) -> None:
    """Wait for outstanding tasks. Called on shutdown so writes are not lost on deploy."""
    if not _running:
        return
    await asyncio.wait(set(_running), timeout=timeout_seconds)
