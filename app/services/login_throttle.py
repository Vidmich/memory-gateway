"""Login backoff, counted per IP and per email.

Two counters because they stop different attacks. The per-IP counter slows a single
source spraying many accounts; the per-email counter slows a distributed attack against
one account, which no per-IP limit can see. Either one tripping is enough to refuse.

The counting itself lives behind :class:`ThrottleStore` so the policy can be tested
without a Redis, and so task 14 — which introduces the real rate limiter — can supply a
better-behaved store without touching this file. In production the store is Redis:
counters have to be shared across replicas, and an in-process one would multiply the
effective limit by the replica count.

When the store is unreachable this **fails open**: the attempt is allowed and a warning
is logged. That is the less bad of two bad options. Failing closed would mean a Redis
hiccup locks every operator out of the UI — during an incident, which is exactly when
they need it — and `/readyz` already takes an instance with no Redis out of the load
balancer, so the window is small. Failing open loses defence in depth for that window;
the passwords are still Argon2id, and nothing else about authentication depends on it.

This is deliberately not the last word on it. Task 18 adds CAPTCHA and alerting; what is
here is the floor, and it is the floor that matters — an unthrottled login endpoint is an
offline password cracker with a network interface.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "login-throttle"


class TooManyAttempts(Exception):
    """Raised instead of attempting the credential check."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many login attempts")
        self.retry_after_seconds = max(1, retry_after_seconds)


class ThrottleStore(Protocol):
    """A counter with a TTL. Small on purpose — Redis implements each method in one
    round trip, and anything richer would be a rate limiter, which task 14 owns."""

    async def increment(self, key: str, ttl_seconds: int) -> int:
        """Add one and return the new value, starting a window if there is none."""

    async def peek(self, key: str) -> tuple[int, int]:
        """Current value and remaining TTL, without touching either."""

    async def delete(self, key: str) -> None: ...


class RedisThrottleStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def increment(self, key: str, ttl_seconds: int) -> int:
        # INCR then EXPIRE in one round trip. `nx=True` anchors the window to the first
        # failure, so an attacker cannot slide it forward by continuing to fail.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_seconds, nx=True)
            count, _ = await pipe.execute()
        return int(count)

    async def peek(self, key: str) -> tuple[int, int]:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.ttl(key)
            value, ttl = await pipe.execute()
        return (int(value) if value else 0, max(int(ttl), 0))

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)


class MemoryThrottleStore:
    """Process-local store, for tests and for a single-process dev run.

    Not correct across replicas — that is what :class:`RedisThrottleStore` is for.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[int, float]] = {}

    def _live(self, key: str) -> tuple[int, float] | None:
        entry = self._counts.get(key)
        if entry is None:
            return None
        if entry[1] <= time.monotonic():
            del self._counts[key]
            return None
        return entry

    async def increment(self, key: str, ttl_seconds: int) -> int:
        entry = self._live(key)
        if entry is None:
            self._counts[key] = (1, time.monotonic() + ttl_seconds)
            return 1
        count, expires_at = entry
        self._counts[key] = (count + 1, expires_at)
        return count + 1

    async def peek(self, key: str) -> tuple[int, int]:
        entry = self._live(key)
        if entry is None:
            return (0, 0)
        return (entry[0], max(1, int(entry[1] - time.monotonic())))

    async def delete(self, key: str) -> None:
        self._counts.pop(key, None)


@dataclass(frozen=True, slots=True)
class Attempt:
    """Who is trying to log in. Both parts are optional: a request can arrive without a
    resolvable client IP behind some proxies, and the email is absent on a malformed body."""

    email: str | None
    ip: str | None


class LoginThrottle:
    def __init__(self, store: ThrottleStore, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings or get_settings()

    async def check(self, attempt: Attempt) -> None:
        """Raise :class:`TooManyAttempts` if this attempt should not even be tried.

        Checked *before* the password is verified, so a locked-out attacker cannot use
        the endpoint's response time as an oracle either.
        """
        for key in self._keys(attempt):
            try:
                count, ttl = await self._store.peek(key)
            except Exception:
                self._unavailable("checking")
                return
            if count >= self._settings.login_max_attempts:
                raise TooManyAttempts(ttl or self._settings.login_lockout_seconds)

    async def record_failure(self, attempt: Attempt) -> None:
        window = self._settings.login_attempt_window_seconds
        for key in self._keys(attempt):
            try:
                count = await self._store.increment(key, window)
            except Exception:
                self._unavailable("recording a failure")
                return
            if count == self._settings.login_max_attempts:
                logger.warning(
                    "login attempts throttled",
                    # The key carries a hash, not the address: this line is written on
                    # every lockout, and a log full of addresses is a stuffing list.
                    extra={"throttle_key": key, "attempts": count},
                )

    async def record_success(self, attempt: Attempt) -> None:
        """Clear the per-email counter only.

        The per-IP counter survives on purpose. An attacker who owns one valid account
        could otherwise reset their own IP budget between guesses at everyone else's.
        """
        if not attempt.email:
            return
        try:
            await self._store.delete(self._email_key(attempt.email))
        except Exception:
            # Nothing to do about it. The stale counter expires on its own, and the
            # worst case is one user having to wait out a window they already cleared.
            self._unavailable("clearing a counter")

    async def unlock(self, attempt: Attempt) -> int:
        """Clear the counters for an email, an address, or both. Returns how many were set.

        The documented unlock path (task 18): a locked-out administrator otherwise waits
        out ``LOGIN_LOCKOUT_SECONDS``, which during an incident is the wrong answer —
        they are locked out precisely because somebody has been attacking the account they
        now need. Reached through ``python -m app.cli unlock-login``, which is a shell on
        the deployment host rather than an endpoint: an unlock endpoint is a way to reset
        the counter that the attacker also has.
        """
        cleared = 0
        for key in self._keys(attempt):
            count, _ = await self._store.peek(key)
            if count:
                cleared += 1
            await self._store.delete(key)
        return cleared

    def _unavailable(self, during: str) -> None:
        """The store is down. See the module docstring for why this is not fatal.

        A broad ``except`` on purpose: redis-py raises its own errors *and* bare
        ``OSError`` on a refused connection, and the response to any of them is the same.
        """
        logger.warning(
            "login throttle unavailable; allowing the attempt",
            extra={"during": during},
            exc_info=True,
        )

    def _keys(self, attempt: Attempt) -> tuple[str, ...]:
        keys = []
        if attempt.email:
            keys.append(self._email_key(attempt.email))
        if attempt.ip:
            keys.append(f"{KEY_PREFIX}:ip:{attempt.ip}")
        return tuple(keys)

    @staticmethod
    def _email_key(email: str) -> str:
        # Hashed so the key space carries no plaintext addresses; lower-cased first
        # because the column is case-insensitive and the counter must be too.
        digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]
        return f"{KEY_PREFIX}:email:{digest}"
