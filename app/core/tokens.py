"""Control-plane tokens: a signed access JWT and an opaque refresh token.

Two different mechanisms on purpose.

The **access token** is a short-lived JWT so that authenticating a request costs a
signature check rather than a database round trip. The cost of that choice is that it
cannot be revoked before it expires, which is why the TTL is 15 minutes and why nothing
irreversible is authorised by the access token alone.

The **refresh token** is opaque — 32 random bytes, stored as a SHA-256 hash, exactly
like the data-plane API keys. It is a database lookup every time, which is what makes
rotation, revocation, and theft detection possible at all. A signed refresh JWT could do
none of those things.

Neither is a place for secrets: a JWT is signed, not encrypted, and its payload is
readable by anyone holding the token.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings, get_settings

ISSUER = "memory-gateway"
#: Distinguishes an access token from any other JWT this service ever signs with the
#: same key. Without it, a token minted for another purpose could be replayed here.
TOKEN_TYPE = "access"
ALGORITHM = "HS256"

REFRESH_TOKEN_BYTES = 32


class InvalidToken(Exception):
    """The token is missing, malformed, expired, or not one of ours."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """The subset of the JWT payload the application acts on."""

    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    role: str
    session_id: uuid.UUID
    jti: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NewRefreshToken:
    token: str
    token_hash: str


def issue_access_token(
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None,
    role: str,
    session_id: uuid.UUID,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Sign an access token. Returns the token and its expiry, so the caller can tell
    the client when to refresh instead of waiting for a 401."""
    settings = settings or get_settings()
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=settings.access_token_ttl_seconds)

    payload = {
        "iss": ISSUER,
        "typ": TOKEN_TYPE,
        "sub": str(user_id),
        "org": str(organization_id) if organization_id else None,
        "role": role,
        "sid": str(session_id),
        "jti": str(uuid.uuid4()),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_signing_key, algorithm=ALGORITHM), expires_at


def decode_access_token(token: str, settings: Settings | None = None) -> AccessClaims:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_signing_key,
            # Pinning the algorithm is what stops the `alg: none` and
            # HMAC-verified-with-a-public-key families of forgery.
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc

    if payload.get("typ") != TOKEN_TYPE:
        raise InvalidToken("not an access token")

    try:
        return AccessClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org"]) if payload.get("org") else None,
            role=str(payload["role"]),
            session_id=uuid.UUID(payload["sid"]),
            jti=uuid.UUID(payload["jti"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidToken("malformed claims") from exc


def mint_refresh_token() -> NewRefreshToken:
    return mint_opaque_token()


def mint_opaque_token() -> NewRefreshToken:
    """A bearer token with no structure: 256 random bits, stored only as a hash.

    Refresh tokens and invitation links are the same construction for the same reason —
    both are handed to a client that must present them back verbatim, and neither has
    anything worth putting inside them.
    """
    token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return NewRefreshToken(token=token, token_hash=hash_refresh_token(token))


def hash_refresh_token(token: str) -> str:
    """SHA-256, unsalted and unstretched — deliberately.

    The token is 256 bits of CSPRNG output, so there is no dictionary to attack and
    nothing for a slow KDF to defend. It is also verified on every refresh, where a
    per-request Argon2 would be a self-inflicted denial of service.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
