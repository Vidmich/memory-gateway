"""Data-plane authentication.

The token carries its own row id, so this is one primary-key lookup and one hash compare
(see ``app/core/keys.py``). Every failure mode returns the same message and the same
amount of work: an unknown key id is still compared against a dummy hash, so response
timing does not tell an attacker which half of the token they got right.

**The key row is read on every request, never cached.** That is the whole mechanism behind
"revoking a key stops the next request": the gateway's *configuration* sits behind a Redis
cache with a 60-second backstop, and if revocation rode along with it a key would keep
working for up to a minute after somebody pressed the button in an incident. One indexed
lookup is a small price for that sentence being true without an asterisk.

``last_used_at`` is the opposite trade. Nothing depends on it being current to the second,
and writing it per request turns every completion into a database write, so
:class:`LastUsedRecorder` gates it behind a Redis key with a one-minute TTL: the first
request in a window writes, the rest cost one ``SET NX``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.proxy.errors import AuthenticationFailed
from app.core import keys
from app.core.background import spawn
from app.core.config import Settings, get_settings
from app.db.models import ApiKey

logger = logging.getLogger(__name__)

_INVALID = "Incorrect API key provided."

# sha256 of a value no key will ever have, so the comparison for a missing row costs the
# same as one for a real key.
_DUMMY_HASH = keys.hash_secret("\x00 no such key \x00")

#: How stale ``last_used_at`` is allowed to be. A minute is well inside what the screen
#: showing it means by "last used", and turns a hot key's write rate into 1/minute.
LAST_USED_INTERVAL_SECONDS = 60
_LAST_USED_PREFIX = "api-key-touched:"


@dataclass(frozen=True)
class AuthenticatedKey:
    id: uuid.UUID
    gateway_id: uuid.UUID
    name: str
    prefix: str


class Recorder(Protocol):
    """Decides whether this key's ``last_used_at`` is due for a write."""

    async def should_write(self, key_id: uuid.UUID) -> bool: ...


class LastUsedRecorder:
    """One write per key per window, coordinated in Redis so replicas agree.

    Fails **open** — a Redis outage means the timestamp is written every request, which
    is the old behaviour and costs a write, rather than never, which would make the column
    quietly wrong for the duration of an incident.
    """

    def __init__(self, redis: Redis, *, settings: Settings | None = None) -> None:
        self._redis = redis
        self._interval = LAST_USED_INTERVAL_SECONDS
        self._settings = settings or get_settings()

    async def should_write(self, key_id: uuid.UUID) -> bool:
        try:
            # `nx=True` makes this the claim *and* the check in one round trip: whoever
            # sets the key owns the write for this window.
            claimed = await self._redis.set(
                f"{_LAST_USED_PREFIX}{key_id}", "1", ex=self._interval, nx=True
            )
        except Exception:
            logger.warning("last-used recorder unavailable; writing anyway", exc_info=True)
            return True
        return bool(claimed)


class AlwaysRecord:
    """The default when no Redis is wired — a single-process dev run, and tests."""

    async def should_write(self, key_id: uuid.UUID) -> bool:
        return True


class KeyAuthenticator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        recorder: Recorder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._recorder = recorder or AlwaysRecord()

    async def authenticate(self, token: str | None) -> AuthenticatedKey:
        parsed = keys.parse(token) if token else None

        record: ApiKey | None = None
        if parsed is not None:
            async with self._session_factory() as session:
                record = await session.get(ApiKey, parsed.key_id)

        secret = parsed.secret if parsed is not None else ""
        matches = keys.verify(secret, record.key_hash if record is not None else _DUMMY_HASH)
        if record is None or not matches:
            raise AuthenticationFailed(_INVALID)

        if record.revoked_at is not None:
            # Distinct from "incorrect": the caller demonstrably holds the key, so telling
            # them it was revoked leaks nothing and saves a support ticket.
            raise AuthenticationFailed("This API key has been revoked.")

        if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            # Same reasoning, and the date is included because "when" is the first thing
            # anyone asks and it is already known to whoever holds the key.
            raise AuthenticationFailed(
                f"This API key expired on {record.expires_at.date().isoformat()}."
            )

        self._touch(record.id)
        return AuthenticatedKey(
            id=record.id,
            gateway_id=record.gateway_id,
            name=record.name,
            prefix=record.prefix,
        )

    def _touch(self, key_id: uuid.UUID) -> None:
        """Stamp ``last_used_at`` without making the caller wait for a write."""
        spawn(self._write_last_used(key_id), name=f"touch-api-key:{key_id}")

    async def _write_last_used(self, key_id: uuid.UUID) -> None:
        if not await self._recorder.should_write(key_id):
            return
        async with self._session_factory() as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=datetime.now(UTC))
            )
            await session.commit()


__all__ = [
    "LAST_USED_INTERVAL_SECONDS",
    "AlwaysRecord",
    "AuthenticatedKey",
    "KeyAuthenticator",
    "LastUsedRecorder",
    "Recorder",
]
