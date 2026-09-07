"""A fixed-window limiter for control-plane actions that cost somebody else money.

Not the request-rate limiter — that is task 14, and it works per gateway and per API key
on the data plane. This is the small thing that stands between an authenticated operator
and the "Test connection" button: every press is an outbound call to a provider, so
without a ceiling one bored tab can turn into a lot of requests to OpenAI billed to the
organization that configured the model.

It reuses :class:`~app.services.login_throttle.ThrottleStore` rather than defining a
second counter abstraction, which means it is Redis-backed in production and shares the
limit across replicas — an in-process counter would multiply the ceiling by the replica
count, which for a spend control is the wrong direction to be wrong in.

Like the login throttle it **fails open** when the store is unreachable, and for the same
reason: a Redis hiccup should not make the configuration screen unusable during an
incident. The exposure is bounded by what the action costs, which here is one token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.errors import RateLimited
from app.services.login_throttle import ThrottleStore

logger = logging.getLogger(__name__)

KEY_PREFIX = "rate-limit"


@dataclass(frozen=True, slots=True)
class FixedWindowLimiter:
    """``limit`` actions per ``window_seconds``, counted per key.

    Fixed window rather than a sliding one on purpose: the failure mode of a fixed window
    is that a caller can get up to twice the limit across a boundary, and at this scale
    twenty probes instead of ten is not worth a sorted set per user.
    """

    store: ThrottleStore
    action: str
    limit: int
    window_seconds: int

    async def check(self, subject: str) -> None:
        """Count this attempt, and raise :class:`RateLimited` once over the ceiling."""
        key = f"{KEY_PREFIX}:{self.action}:{subject}"
        try:
            count = await self.store.increment(key, self.window_seconds)
        except Exception:
            # See the module docstring. A broad except because redis-py raises both its
            # own errors and bare OSError on a refused connection.
            logger.warning(
                "rate limiter unavailable; allowing the action",
                extra={"action": self.action},
                exc_info=True,
            )
            return

        if count <= self.limit:
            return

        try:
            _, ttl = await self.store.peek(key)
        except Exception:
            ttl = 0
        raise RateLimited(
            "Too many attempts. Wait a moment and try again.",
            retry_after_seconds=ttl or self.window_seconds,
        )
