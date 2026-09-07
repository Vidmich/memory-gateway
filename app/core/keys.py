"""Data-plane API keys: minting, parsing, and verification.

The token embeds its own row id — ``mg_<key_id>_<secret>`` — so authenticating is a
single primary-key lookup followed by one hash comparison. The alternative, hashing the
whole token and scanning, either needs an index over every key or degrades as the table
grows.

Only ``sha256(secret)`` is stored. The secret is high-entropy random, so the usual reason
for a slow password hash (guessable inputs) does not apply, and a per-request Argon2 would
put tens of milliseconds into a path budgeted at 150 ms total (SPEC §4.2).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

PREFIX = "mg_"
SECRET_BYTES = 32  # 256 bits


@dataclass(frozen=True)
class NewApiKey:
    """A freshly minted key. ``token`` is the only time the plaintext exists."""

    key_id: uuid.UUID
    token: str
    key_hash: str
    prefix: str


@dataclass(frozen=True)
class ParsedApiKey:
    key_id: uuid.UUID
    secret: str


def mint(key_id: uuid.UUID) -> NewApiKey:
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return NewApiKey(
        key_id=key_id,
        token=f"{PREFIX}{key_id.hex}_{secret}",
        key_hash=hash_secret(secret),
        prefix=display_prefix(key_id),
    )


def parse(token: str) -> ParsedApiKey | None:
    """Split a bearer token, or return ``None`` if it is not one of ours.

    ``maxsplit=2`` matters: ``token_urlsafe`` can emit ``_``, and the secret must survive
    intact.
    """
    if not token.startswith(PREFIX):
        return None

    parts = token.split("_", 2)
    if len(parts) != 3:
        return None

    _, raw_id, secret = parts
    if not secret:
        return None

    try:
        key_id = uuid.UUID(hex=raw_id)
    except ValueError:
        return None

    return ParsedApiKey(key_id=key_id, secret=secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify(secret: str, key_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), key_hash)


def display_prefix(key_id: uuid.UUID) -> str:
    """What the UI shows for a key it can never display again. Carries no secret."""
    return f"{PREFIX}{key_id.hex[:8]}"


def bearer_token(header_value: str | None) -> str | None:
    """Extract the credential from an ``Authorization`` header, case-insensitively."""
    if not header_value:
        return None
    scheme, _, credential = header_value.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()
