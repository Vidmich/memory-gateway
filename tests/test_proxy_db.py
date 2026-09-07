"""The parts that only a real PostgreSQL can check: the schema's constraints, the
resolver's SQL, and the seed command.

Marked ``db``; skipped when no server is reachable, required in CI via
``REQUIRE_DB_TESTS=1``.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.proxy.errors import AuthenticationFailed, GatewayNotFound, GatewayUnavailable
from app.cli import DEMO_SLUG, SeedOptions, seed_demo
from app.core import background, keys
from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.core.passwords import Hasher
from app.db.models import ApiKey, Gateway, GatewayTarget, Organization, UpstreamModel
from app.services.api_keys import KeyAuthenticator
from app.services.gateways import GatewayResolver

pytestmark = pytest.mark.db

CREDENTIAL = "sk-provider-secret"


@pytest.fixture
def secret_box() -> SecretBox:
    return SecretBox(master_key=os.urandom(32))


async def build_gateway(
    session: AsyncSession,
    secret_box: SecretBox,
    *,
    slug: str = "demo",
    enabled: bool = True,
    model_enabled: bool = True,
    ciphertext: bytes | None = None,
) -> tuple[Gateway, UpstreamModel, str]:
    organization = Organization(id=uuid7(), name="Acme", slug=f"org-{uuid7().hex[:8]}")
    session.add(organization)
    await session.flush()

    model = UpstreamModel(
        id=uuid7(),
        organization_id=organization.id,
        scope="org",
        name=f"model-{uuid7().hex[:8]}",
        base_url="https://api.example.com/v1",
        dialect="openai",
        upstream_model_id="gpt-4o-mini",
        auth_type="bearer",
        credential_ciphertext=(
            ciphertext if ciphertext is not None else secret_box.encrypt(CREDENTIAL)
        ),
        system_context="You are terse.",
        default_params={"temperature": 0.2},
        timeout_seconds=45,
        enabled=model_enabled,
    )
    gateway = Gateway(
        id=uuid7(),
        organization_id=organization.id,
        slug=slug,
        name="Demo",
        enabled=enabled,
        param_overrides={"top_p": 0.9},
    )
    session.add_all([model, gateway])
    await session.flush()

    session.add(
        GatewayTarget(id=uuid7(), gateway_id=gateway.id, upstream_model_id=model.id, priority=0)
    )
    minted = keys.mint(uuid7())
    session.add(
        ApiKey(
            id=minted.key_id,
            gateway_id=gateway.id,
            name="demo",
            key_hash=minted.key_hash,
            prefix=minted.prefix,
        )
    )
    await session.flush()
    return gateway, model, minted.token


# -- schema ------------------------------------------------------------------


async def test_rows_round_trip_through_the_migrated_schema(
    db_session: AsyncSession, secret_box: SecretBox
) -> None:
    gateway, model, _ = await build_gateway(db_session, secret_box)

    stored = await db_session.get(UpstreamModel, model.id)

    assert stored is not None
    assert stored.default_params == {"temperature": 0.2}
    assert stored.credential_ciphertext is not None
    assert secret_box.decrypt(stored.credential_ciphertext) == CREDENTIAL
    assert stored.created_at is not None
    assert (await db_session.get(Gateway, gateway.id)) is not None


async def test_a_global_model_may_not_belong_to_an_organization(
    db_session: AsyncSession,
) -> None:
    """SPEC §5.3 makes this an isolation boundary, so the database enforces it."""
    organization = Organization(id=uuid7(), name="Acme", slug=f"org-{uuid7().hex[:8]}")
    db_session.add(organization)
    await db_session.flush()

    db_session.add(
        UpstreamModel(
            id=uuid7(),
            organization_id=organization.id,
            scope="global",
            name="bad",
            base_url="https://x/v1",
            dialect="openai",
            upstream_model_id="m",
            auth_type="none",
            timeout_seconds=60,
            enabled=True,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_org_model_must_name_its_organization(db_session: AsyncSession) -> None:
    db_session.add(
        UpstreamModel(
            id=uuid7(),
            organization_id=None,
            scope="org",
            name="bad",
            base_url="https://x/v1",
            dialect="openai",
            upstream_model_id="m",
            auth_type="none",
            timeout_seconds=60,
            enabled=True,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("field", "value"),
    [("dialect", "bedrock"), ("auth_type", "oauth2"), ("scope", "team"), ("timeout_seconds", 0)],
)
async def test_enumerated_columns_are_constrained(
    db_session: AsyncSession, field: str, value: Any
) -> None:
    organization = Organization(id=uuid7(), name="Acme", slug=f"org-{uuid7().hex[:8]}")
    db_session.add(organization)
    await db_session.flush()

    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": organization.id,
        "scope": "org",
        "name": "m",
        "base_url": "https://x/v1",
        "dialect": "openai",
        "upstream_model_id": "m",
        "auth_type": "none",
        "timeout_seconds": 60,
        "enabled": True,
    }
    values[field] = value
    db_session.add(UpstreamModel(**values))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_gateway_slugs_are_unique(db_session: AsyncSession, secret_box: SecretBox) -> None:
    await build_gateway(db_session, secret_box, slug="taken")

    with pytest.raises(IntegrityError):
        await build_gateway(db_session, secret_box, slug="taken")


# -- resolver ----------------------------------------------------------------


async def test_resolver_returns_a_decrypted_target(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    gateway, _model, _ = await build_gateway(db_session, secret_box, slug="resolvable")

    resolved = await GatewayResolver(db_session_factory, secret_box).resolve("resolvable")

    assert resolved.id == gateway.id
    assert resolved.virtual_model == "resolvable"
    assert resolved.param_overrides == {"top_p": 0.9}
    target = resolved.target()
    assert target.credential == CREDENTIAL
    assert target.upstream_model_id == "gpt-4o-mini"
    assert target.system_context == "You are terse."
    assert target.default_params == {"temperature": 0.2}
    assert target.timeout_seconds == 45


async def test_resolver_does_not_leak_the_credential_in_a_repr(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    await build_gateway(db_session, secret_box, slug="quiet")

    resolved = await GatewayResolver(db_session_factory, secret_box).resolve("quiet")

    assert CREDENTIAL not in repr(resolved.target())


async def test_unknown_slug_raises_not_found(
    db_session_factory: async_sessionmaker[AsyncSession], secret_box: SecretBox
) -> None:
    with pytest.raises(GatewayNotFound):
        await GatewayResolver(db_session_factory, secret_box).resolve("no-such-gateway")


async def test_a_disabled_gateway_is_unavailable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    await build_gateway(db_session, secret_box, slug="off", enabled=False)

    with pytest.raises(GatewayUnavailable):
        await GatewayResolver(db_session_factory, secret_box).resolve("off")


async def test_a_disabled_model_is_skipped_rather_than_returned(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    await build_gateway(db_session, secret_box, slug="empty", model_enabled=False)

    resolved = await GatewayResolver(db_session_factory, secret_box).resolve("empty")

    assert resolved.targets == ()
    with pytest.raises(GatewayUnavailable):
        resolved.target()


async def test_a_credential_from_another_master_key_fails_loudly(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    """Calling the provider with no credential and reporting its 401 would point the
    operator at the client's key instead of at the master key."""
    other = SecretBox(master_key=os.urandom(32))
    await build_gateway(
        db_session, secret_box, slug="rotated", ciphertext=other.encrypt(CREDENTIAL)
    )

    with pytest.raises(GatewayUnavailable):
        await GatewayResolver(db_session_factory, secret_box).resolve("rotated")


# -- authenticator -----------------------------------------------------------


async def test_authenticator_accepts_a_stored_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    gateway, _, token = await build_gateway(db_session, secret_box, slug="authed")

    result = await KeyAuthenticator(db_session_factory).authenticate(token)

    assert result.gateway_id == gateway.id


async def test_authenticator_stamps_last_used_at(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    _, _, token = await build_gateway(db_session, secret_box, slug="stamped")
    key_id = uuid.UUID(token.split("_", 2)[1])
    assert (await db_session.get(ApiKey, key_id)).last_used_at is None  # type: ignore[union-attr]

    await KeyAuthenticator(db_session_factory).authenticate(token)
    await background.drain(timeout_seconds=5)

    record = await db_session.get(ApiKey, key_id)
    assert record is not None
    await db_session.refresh(record)
    assert record.last_used_at is not None


async def test_authenticator_rejects_a_revoked_key(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    _, _, token = await build_gateway(db_session, secret_box, slug="revoked")
    key_id = uuid.UUID(token.split("_", 2)[1])
    record = await db_session.get(ApiKey, key_id)
    assert record is not None
    record.revoked_at = func.now()
    await db_session.flush()

    with pytest.raises(AuthenticationFailed, match="revoked"):
        await KeyAuthenticator(db_session_factory).authenticate(token)


async def test_authenticator_rejects_an_unknown_key(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(AuthenticationFailed):
        await KeyAuthenticator(db_session_factory).authenticate(keys.mint(uuid7()).token)


# -- seed --------------------------------------------------------------------


def seed_options(model: str = "gpt-4o-mini") -> SeedOptions:
    return SeedOptions(
        base_url="https://api.example.com/v1",
        model=model,
        credential=CREDENTIAL,
        auth_type="bearer",
        rotate_key=False,
    )


@pytest.fixture
def hasher() -> Hasher:
    """Deliberately weak. These tests are about the seed, not about Argon2."""
    return Hasher(time_cost=1, memory_cost_kib=8, parallelism=1)


async def _count(session: AsyncSession, model: type[Any]) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_seed_creates_a_working_demo_gateway(
    db_session: AsyncSession, secret_box: SecretBox, hasher: Hasher
) -> None:
    token = (await seed_demo(db_session, seed_options(), secret_box, hasher)).api_key

    assert token is not None
    gateway = (
        await db_session.execute(select(Gateway).where(Gateway.slug == DEMO_SLUG))
    ).scalar_one()
    assert gateway.enabled
    parsed = keys.parse(token)
    assert parsed is not None
    key = await db_session.get(ApiKey, parsed.key_id)
    assert key is not None
    assert key.gateway_id == gateway.id


async def test_seed_is_idempotent(
    db_session: AsyncSession, secret_box: SecretBox, hasher: Hasher
) -> None:
    await seed_demo(db_session, seed_options(), secret_box, hasher)
    before = [await _count(db_session, model) for model in (Organization, UpstreamModel, Gateway)]

    second = await seed_demo(db_session, seed_options("gpt-4o"), secret_box, hasher)

    assert [
        await _count(db_session, model) for model in (Organization, UpstreamModel, Gateway)
    ] == before
    assert await _count(db_session, GatewayTarget) == 1
    assert second.api_key is None, "an existing key is kept: its plaintext cannot be reshown"
    assert second.admin_password is None, "and neither can the superadmin's password"


async def test_seed_updates_the_upstream_in_place(
    db_session: AsyncSession, secret_box: SecretBox, hasher: Hasher
) -> None:
    await seed_demo(db_session, seed_options(), secret_box, hasher)

    await seed_demo(db_session, seed_options("gpt-4o"), secret_box, hasher)

    model = (await db_session.execute(select(UpstreamModel))).scalar_one()
    assert model.upstream_model_id == "gpt-4o"


async def test_rotating_revokes_the_previous_key(
    db_session: AsyncSession, secret_box: SecretBox, hasher: Hasher
) -> None:
    first = (await seed_demo(db_session, seed_options(), secret_box, hasher)).api_key
    assert first is not None

    options = seed_options()
    options.rotate_key = True
    second = (await seed_demo(db_session, options, secret_box, hasher)).api_key

    assert second is not None and second != first
    parsed = keys.parse(first)
    assert parsed is not None
    old = await db_session.get(ApiKey, parsed.key_id)
    assert old is not None
    assert old.revoked_at is not None
