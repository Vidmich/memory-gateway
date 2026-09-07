"""Data-plane authentication.

The token carries its own row id, so this is one primary-key lookup and one hash compare
(see ``app/core/keys.py``). Every failure mode returns the same message and the same
amount of work: an unknown key id is still compared against a dummy hash, so response
timing does not tell an attacker which half of the token they got right.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.proxy.errors import AuthenticationFailed
from app.core import keys
from app.core.background import spawn
from app.db.models import ApiKey

logger = logging.getLogger(__name__)

_INVALID = "Incorrect API key provided."

# sha256 of a value no key will ever have, so the comparison for a missing row costs the
# same as one for a real key.
_DUMMY_HASH = keys.hash_secret("\x00 no such key \x00")


@dataclass(frozen=True)
class AuthenticatedKey:
    id: uuid.UUID
    gateway_id: uuid.UUID
    name: str
    prefix: str


class KeyAuthenticator:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            await session.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=datetime.now(UTC))
            )
            await session.commit()
