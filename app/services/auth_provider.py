"""The seam that keeps OIDC (SPEC §16.7) from being a rewrite.

Everything above this file — the login route, the session service, the ``CurrentUser``
dependency — talks to an :class:`AuthProvider`, never to a password. Adding OIDC then
means adding one implementation and one route, rather than editing every place that
assumed a password existed.

Two methods, because there are exactly two questions to answer:

``authenticate``      Who is this, given credentials they just presented?
``user_from_claims``  Who is this, given a token we issued earlier?

The second is not redundant. An OIDC provider provisions users just in time, so the first
request bearing a valid token may be the first time this system has heard of the person.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.passwords import Hasher
from app.core.tokens import AccessClaims
from app.db.models import User
from app.services.auth_store import AuthTransaction

logger = logging.getLogger(__name__)

#: Hashed once and compared against when no user matches, so that "unknown email" and
#: "wrong password" cost the same. Built lazily: hashing at import time would put an
#: Argon2id run inside every process start, including the migration job's.
_DUMMY_PASSWORD = "not-a-real-password-0000"


@dataclass(frozen=True, slots=True)
class PasswordCredentials:
    email: str
    password: str


#: What providers accept. A union once there is more than one shape (SPEC §16.7).
Credentials = PasswordCredentials


class AuthProvider(Protocol):
    #: Recorded in the login log line, and shown in the UI once more than one exists.
    name: str

    async def authenticate(
        self, transaction: AuthTransaction, credentials: Credentials
    ) -> User | None: ...

    async def user_from_claims(
        self, transaction: AuthTransaction, claims: AccessClaims
    ) -> User | None: ...


class LocalPasswordProvider:
    """Email and password against the ``users`` table."""

    name = "local"

    def __init__(self, hasher: Hasher) -> None:
        self._hasher = hasher
        self._dummy_hash: str | None = None

    async def authenticate(
        self, transaction: AuthTransaction, credentials: Credentials
    ) -> User | None:
        user = await transaction.user_by_email(credentials.email)

        if user is None or not user.can_log_in_with_password:
            # Verify anyway. Without this, "no such user" returns in microseconds while a
            # real user costs a full Argon2id hash, and that difference is a user
            # enumeration oracle no amount of generic error text hides.
            self._hasher.verify(self._dummy(), credentials.password)
            return None

        assert user.password_hash is not None  # can_log_in_with_password
        ok, upgraded = self._hasher.verify_and_upgrade(user.password_hash, credentials.password)
        if not ok:
            return None

        if upgraded is not None:
            # The cost parameters changed since this hash was written. Login is the only
            # moment the plaintext exists, so it is the only moment this can happen.
            logger.info("password hash upgraded", extra={"user_id": str(user.id)})
            user.password_hash = upgraded

        if not user.is_active:
            # Checked after verification, again for timing: a suspended account must not
            # answer faster than an active one.
            return None
        return user

    async def user_from_claims(
        self, transaction: AuthTransaction, claims: AccessClaims
    ) -> User | None:
        user = await transaction.user_by_id(claims.user_id)
        if user is None or not user.is_active:
            return None
        return user

    def _dummy(self) -> str:
        if self._dummy_hash is None:
            self._dummy_hash = self._hasher.hash(_DUMMY_PASSWORD)
        return self._dummy_hash
