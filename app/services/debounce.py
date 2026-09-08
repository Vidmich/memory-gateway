"""Coalescing a burst of events into one job (SPEC §6.4, step 1).

A conversation is not one request. Somebody asking six questions in two minutes produces
six transcripts, and distilling each of them separately would cost six model calls to learn
one thing — while also being *worse*, because the sixth question is only interpretable
beside the first five.

So a turn does not enqueue a pass; it **arms** one. :meth:`Debouncer.arm` writes a fresh
token at ``(end_user, session)`` and the caller enqueues a job carrying that token, delayed
by the debounce window. The next turn overwrites the token and enqueues another. When each
job eventually runs it calls :meth:`Debouncer.claim`, and only the job holding the *current*
token proceeds — every earlier one exits having done nothing. The pass therefore happens
once, a debounce window after the conversation goes quiet, over everything that accumulated
in it.

That is a trailing debounce, and it is the reading of SPEC §6.4 that matches its stated
purpose. A leading window — first turn claims, later turns are absorbed — would produce
"exactly one job" per window rather than several cheap no-ops, which is tidier and wrong: a
ten-minute conversation would then be distilled twenty times, in windows that each cut
across the middle of an exchange.

**The token is checked, not locked.** Redis has no atomic compare-and-delete without
scripting, so two jobs holding the same token can both claim it in the same instant. The
window is small and the consequence is bounded: the reconciliation itself runs under
:mod:`app.services.locks`, and every write in it is idempotent-by-dedupe. This is the same
trade — and the same argument — as ``RedisLock._release``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Protocol

logger = logging.getLogger(__name__)

KEY_PREFIX = "distil:"

#: How much longer than the debounce window a token lives. The job has to run, read its
#: transcripts and finish while its own token is still there; a lease that expired first
#: would let a *stale* job win the claim. Generous, because the cost of a token outliving
#: its usefulness is one no-op job.
TTL_SLACK_SECONDS = 300


def session_key(end_user_id: Any, session_id: str | None) -> str:
    """SPEC §6.4's coalescing key. A conversation with no session id is still one thread
    as far as debouncing is concerned — the alternative is a pass per turn for exactly the
    clients that already tell us least."""
    return f"{KEY_PREFIX}{end_user_id}:{session_id or '-'}"


class Debouncer(Protocol):
    async def arm(self, key: str, *, ttl_seconds: int) -> str:
        """Register a fresh pending pass and return its token."""
        ...

    async def claim(self, key: str, token: str) -> bool:
        """True if ``token`` is still the pending one — and consume it if so."""
        ...

    async def cancel(self, key: str) -> None:
        """Forget any pending pass. Used by "distil now", which runs immediately and
        should not then be followed by the pass it pre-empted."""
        ...


class RedisDebouncer:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def arm(self, key: str, *, ttl_seconds: int) -> str:
        token = secrets.token_hex(8)
        await self._redis.set(key, token, ex=ttl_seconds + TTL_SLACK_SECONDS)
        return token

    async def claim(self, key: str, token: str) -> bool:
        current = await self._redis.get(key)
        if current is None:
            # Nothing pending. Either somebody already ran this pass or the key expired
            # while the queue was backed up. Running anyway would be harmless — the pass
            # is idempotent — but it would also undo the coalescing under exactly the load
            # that made the coalescing matter.
            return False
        if current != token and current != token.encode():
            return False
        await self._redis.delete(key)
        return True

    async def cancel(self, key: str) -> None:
        await self._redis.delete(key)


class MemoryDebouncer:
    """In-process, no expiry. One event loop, so no atomicity concerns."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    async def arm(self, key: str, *, ttl_seconds: int) -> str:
        token = secrets.token_hex(8)
        self.tokens[key] = token
        return token

    async def claim(self, key: str, token: str) -> bool:
        if self.tokens.get(key) != token:
            return False
        del self.tokens[key]
        return True

    async def cancel(self, key: str) -> None:
        self.tokens.pop(key, None)


class AlwaysClaims:
    """A debouncer that never coalesces. What "distil now" and the backfill run under —
    both are somebody asking for a pass *now*, and a pending token they never armed is not
    theirs to lose to."""

    async def arm(self, key: str, *, ttl_seconds: int) -> str:
        return ""

    async def claim(self, key: str, token: str) -> bool:
        return True

    async def cancel(self, key: str) -> None:
        return None


__all__ = [
    "KEY_PREFIX",
    "TTL_SLACK_SECONDS",
    "AlwaysClaims",
    "Debouncer",
    "MemoryDebouncer",
    "RedisDebouncer",
    "session_key",
]
