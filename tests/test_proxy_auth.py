"""Data-plane authentication, at the route and at the authenticator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.proxy.errors import AuthenticationFailed
from app.core import background, keys
from app.core.ids import uuid7
from app.db.models import ApiKey
from app.services.api_keys import KeyAuthenticator
from tests.conftest import ProxyHarness
from tests.support import Behaviour, completion

BODY = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}


# -- through the route -------------------------------------------------------


async def test_a_valid_key_is_accepted(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers())

    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer "},
        {"Authorization": "mg_abc"},
        {"Authorization": "Basic bWc6YWJj"},
        {"Authorization": "Bearer sk-an-openai-key"},
        {"Authorization": "Bearer mg_not-a-uuid_secret"},
        {"Authorization": f"Bearer mg_{uuid.uuid4().hex}_wrong-secret"},
    ],
    ids=[
        "missing",
        "empty",
        "no-credential",
        "no-scheme",
        "wrong-scheme",
        "not-our-format",
        "malformed-id",
        "unknown-id",
    ],
)
async def test_rejected_credentials_return_401(
    proxy: ProxyHarness, headers: dict[str, str]
) -> None:
    response = await proxy.client.post(proxy.url(), json=BODY, headers=headers)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_api_key"
    assert error["type"] == "invalid_request_error"
    assert proxy.upstream.requests == []


async def test_a_wrong_secret_for_a_real_key_is_rejected(proxy: ProxyHarness) -> None:
    prefix, key_id, _ = proxy.token.split("_", 2)

    response = await proxy.client.post(
        proxy.url(), json=BODY, headers=proxy.headers(f"{prefix}_{key_id}_wrong")
    )

    assert response.status_code == 401


async def test_a_revoked_key_is_rejected(proxy: ProxyHarness) -> None:
    revoked = proxy.authenticator.issue(proxy.gateway.id, revoked=True)

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers(revoked))

    assert response.status_code == 401
    assert "revoked" in response.json()["error"]["message"]


async def test_a_key_for_another_gateway_is_forbidden(proxy: ProxyHarness) -> None:
    """Keys are gateway-scoped; a valid key on the wrong endpoint is a config mistake,
    not an authentication failure, and the status says so."""
    other = proxy.authenticator.issue(uuid7())

    response = await proxy.client.post(proxy.url(), json=BODY, headers=proxy.headers(other))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "key_not_for_gateway"
    assert proxy.upstream.requests == []


async def test_an_unknown_slug_does_not_leak_before_authentication(
    proxy: ProxyHarness,
) -> None:
    """Authenticating first means an anonymous caller cannot tell an existing gateway
    from a missing one by comparing 401 against 404."""
    response = await proxy.client.post(proxy.url(slug="does-not-exist"), json=BODY)

    assert response.status_code == 401


# -- the authenticator itself ------------------------------------------------


class _StubSession:
    """Just enough AsyncSession for the authenticator."""

    def __init__(self, record: ApiKey | None) -> None:
        self.record = record
        self.statements: list[Any] = []
        self.committed = False

    async def get(self, model: type[Any], pk: uuid.UUID) -> ApiKey | None:
        if self.record is not None and self.record.id == pk:
            return self.record
        return None

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _authenticator(session: _StubSession) -> KeyAuthenticator:
    factory = cast("async_sessionmaker[AsyncSession]", lambda: session)
    return KeyAuthenticator(factory)


def _record(key_hash: str, key_id: uuid.UUID, *, revoked: bool = False) -> ApiKey:
    return ApiKey(
        id=key_id,
        gateway_id=uuid7(),
        name="test",
        key_hash=key_hash,
        prefix=keys.display_prefix(key_id),
        revoked_at=datetime.now(UTC) if revoked else None,
    )


async def test_authenticate_returns_the_key_and_its_gateway() -> None:
    minted = keys.mint(uuid7())
    record = _record(minted.key_hash, minted.key_id)
    session = _StubSession(record)

    result = await _authenticator(session).authenticate(minted.token)

    assert result.id == minted.key_id
    assert result.gateway_id == record.gateway_id


async def test_last_used_at_is_written_without_blocking_the_caller() -> None:
    minted = keys.mint(uuid7())
    session = _StubSession(_record(minted.key_hash, minted.key_id))

    await _authenticator(session).authenticate(minted.token)
    assert session.statements == []  # nothing written on the request path

    await background.drain(timeout_seconds=2)

    assert len(session.statements) == 1
    assert session.committed


@pytest.mark.parametrize("token", [None, "", "sk-other", "mg_bad_token"])
async def test_unparseable_tokens_never_touch_the_database(token: str | None) -> None:
    session = _StubSession(None)

    with pytest.raises(AuthenticationFailed):
        await _authenticator(session).authenticate(token)


async def test_unknown_and_wrong_secret_are_indistinguishable() -> None:
    """Different messages here would let an attacker confirm which key ids exist."""
    minted = keys.mint(uuid7())
    known = _StubSession(_record(minted.key_hash, minted.key_id))
    unknown = _StubSession(None)
    _, key_id, _ = minted.token.split("_", 2)

    with pytest.raises(AuthenticationFailed) as wrong_secret:
        await _authenticator(known).authenticate(f"mg_{key_id}_nope")
    with pytest.raises(AuthenticationFailed) as no_such_key:
        await _authenticator(unknown).authenticate(minted.token)

    assert str(wrong_secret.value) == str(no_such_key.value)


async def test_revoked_keys_are_told_they_are_revoked() -> None:
    minted = keys.mint(uuid7())
    session = _StubSession(_record(minted.key_hash, minted.key_id, revoked=True))

    with pytest.raises(AuthenticationFailed, match="revoked"):
        await _authenticator(session).authenticate(minted.token)
