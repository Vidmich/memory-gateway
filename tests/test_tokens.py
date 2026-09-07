"""Access-token signing and decoding, and refresh-token minting."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings, get_settings
from app.core.ids import uuid7
from app.core.tokens import (
    ALGORITHM,
    ISSUER,
    AccessClaims,
    InvalidToken,
    decode_access_token,
    hash_refresh_token,
    issue_access_token,
    mint_refresh_token,
)

USER_ID = uuid7()
ORG_ID = uuid7()
SESSION_ID = uuid7()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


def issue(settings: Settings, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "user_id": USER_ID,
        "organization_id": ORG_ID,
        "role": "org_admin",
        "session_id": SESSION_ID,
        "settings": settings,
    }
    kwargs.update(overrides)
    token, _ = issue_access_token(**kwargs)  # type: ignore[arg-type]
    return token


def test_a_token_round_trips(settings: Settings) -> None:
    claims = decode_access_token(issue(settings), settings)

    assert claims == AccessClaims(
        user_id=USER_ID,
        organization_id=ORG_ID,
        role="org_admin",
        session_id=SESSION_ID,
        jti=claims.jti,
        expires_at=claims.expires_at,
    )


def test_a_superadmin_token_carries_no_organization(settings: Settings) -> None:
    claims = decode_access_token(issue(settings, organization_id=None, role="superadmin"), settings)

    assert claims.organization_id is None
    assert claims.role == "superadmin"


def test_the_expiry_matches_the_configured_ttl(settings: Settings) -> None:
    _, expires_at = issue_access_token(
        user_id=USER_ID,
        organization_id=ORG_ID,
        role="org_admin",
        session_id=SESSION_ID,
        settings=settings,
    )

    remaining = (expires_at - datetime.now(UTC)).total_seconds()

    assert abs(remaining - settings.access_token_ttl_seconds) < 5


def test_every_token_has_a_distinct_jti(settings: Settings) -> None:
    """Task 18 builds a revocation list on it; two tokens sharing one would revoke both."""
    first = decode_access_token(issue(settings), settings)
    second = decode_access_token(issue(settings), settings)

    assert first.jti != second.jti


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(seconds=settings.access_token_ttl_seconds + 60)
    token, _ = issue_access_token(
        user_id=USER_ID,
        organization_id=ORG_ID,
        role="org_admin",
        session_id=SESSION_ID,
        settings=settings,
        now=past,
    )

    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


def test_a_token_signed_with_another_key_is_rejected(settings: Settings) -> None:
    other = settings.model_copy(update={"jwt_signing_key": "a" * 40})

    with pytest.raises(InvalidToken):
        decode_access_token(issue(other), settings)


def test_an_unsigned_token_is_rejected(settings: Settings) -> None:
    """`alg: none` is the oldest JWT forgery there is; pinning the algorithm stops it."""
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "typ": "access",
            "sub": str(USER_ID),
            "org": str(ORG_ID),
            "role": "superadmin",
            "sid": str(SESSION_ID),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


def test_a_token_from_another_issuer_is_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {
            "iss": "somebody-else",
            "typ": "access",
            "sub": str(USER_ID),
            "role": "org_admin",
            "sid": str(SESSION_ID),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_signing_key,
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


def test_a_token_of_another_type_is_rejected(settings: Settings) -> None:
    """Anything else this service ever signs with the same key must not be replayable
    here — which is the entire job of the `typ` claim."""
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "typ": "password-reset",
            "sub": str(USER_ID),
            "role": "org_admin",
            "sid": str(SESSION_ID),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_signing_key,
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


@pytest.mark.parametrize("missing", ["sub", "jti", "exp", "iat", "iss"])
def test_a_token_missing_a_required_claim_is_rejected(settings: Settings, missing: str) -> None:
    payload = {
        "iss": ISSUER,
        "typ": "access",
        "sub": str(USER_ID),
        "role": "org_admin",
        "sid": str(SESSION_ID),
        "jti": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    del payload[missing]

    with pytest.raises(InvalidToken):
        decode_access_token(
            jwt.encode(payload, settings.jwt_signing_key, algorithm=ALGORITHM), settings
        )


def test_a_malformed_subject_is_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "typ": "access",
            "sub": "not-a-uuid",
            "role": "org_admin",
            "sid": str(SESSION_ID),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_signing_key,
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


@pytest.mark.parametrize("token", ["", "not.a.token", "a.b.c", "Bearer something"])
def test_garbage_is_rejected(settings: Settings, token: str) -> None:
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


# -- refresh tokens ----------------------------------------------------------


def test_a_refresh_token_is_unguessable_and_stored_as_a_hash() -> None:
    minted = mint_refresh_token()

    assert len(minted.token) >= 40
    assert minted.token not in minted.token_hash
    assert minted.token_hash == hash_refresh_token(minted.token)
    assert len(minted.token_hash) == 64


def test_two_refresh_tokens_differ() -> None:
    assert mint_refresh_token().token != mint_refresh_token().token
