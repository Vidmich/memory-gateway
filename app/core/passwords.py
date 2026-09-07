"""Password hashing.

Argon2id, with the cost parameters in configuration rather than in code. Two reasons:
the right memory cost depends on the machine the API runs on, and the test suite would
otherwise spend most of its time deliberately burning CPU — every login test pays the
full cost twice.

The stored hash is PHC-encoded, so it carries its own parameters. That is what makes
:func:`verify_and_upgrade` possible: raising the cost is a configuration change plus a
transparent rehash on each user's next login, not a migration and a forced reset.
"""

from __future__ import annotations

from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings, get_settings

#: Argon2id rejects anything longer outright, and a megabyte-long password is a cheap
#: way to make the server do a megabyte of work per login attempt.
MAX_PASSWORD_BYTES = 1024

#: Below this, the strength of the hash stops mattering.
MIN_PASSWORD_LENGTH = 12


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """The one rule enforced here. Anything more (dictionaries, breach lists) belongs
    in task 18, where it can be configured per deployment."""

    min_length: int = MIN_PASSWORD_LENGTH

    def check(self, password: str) -> str | None:
        """Return the reason this password is unacceptable, or ``None``."""
        if len(password) < self.min_length:
            return f"Password must be at least {self.min_length} characters."
        if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
            return "Password is too long."
        return None


class Hasher:
    """Wraps ``argon2-cffi`` so call sites never handle its exception types."""

    def __init__(self, *, time_cost: int, memory_cost_kib: int, parallelism: int) -> None:
        self._hasher = PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            # A corrupt or foreign-format hash is a failed login, not a 500: one bad row
            # must not be able to take the login endpoint down.
            return False

    def verify_and_upgrade(self, password_hash: str, password: str) -> tuple[bool, str | None]:
        """Verify, and return a replacement hash when the stored one is below policy.

        The rehash needs the plaintext, so login is the only moment it can happen.
        """
        if not self.verify(password_hash, password):
            return False, None
        try:
            if self._hasher.check_needs_rehash(password_hash):
                return True, self.hash(password)
        except InvalidHashError:  # pragma: no cover - verify() already rejected these
            return True, self.hash(password)
        return True, None


def build_hasher(settings: Settings | None = None) -> Hasher:
    settings = settings or get_settings()
    return Hasher(
        time_cost=settings.password_time_cost,
        memory_cost_kib=settings.password_memory_cost_kib,
        parallelism=settings.password_parallelism,
    )
