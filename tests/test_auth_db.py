"""Control-plane auth against a real PostgreSQL.

Three things live here that cannot be checked anywhere else: the store contract against
the implementation that ships, the schema constraints (a CHECK is only real if the server
enforces it), and the seeded superadmin.

Skipped without a reachable server; ``REQUIRE_DB_TESTS=1`` turns the skip into a failure,
which is how CI makes sure these actually run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli import DEFAULT_ADMIN_EMAIL, SeedOptions, seed_demo
from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.db.models import Organization, User, UserSession
from app.services.auth import SessionExpired
from app.services.auth_store import PostgresAuthStore
from app.services.login_throttle import (
    Attempt,
    LoginThrottle,
    RedisThrottleStore,
    TooManyAttempts,
)
from tests.auth_store_contract import CHECKS, HASHER, Check, StoreFixture
from tests.auth_support import CONTEXT, PASSWORD, build_auth, make_organization, make_user

pytestmark = pytest.mark.db


@pytest.fixture
async def store(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[StoreFixture]:
    organization = make_organization()
    user = make_user(hasher=HASHER, organization=organization)
    db_session.add(organization)
    db_session.add(user)
    await db_session.flush()

    yield StoreFixture(
        store=PostgresAuthStore(db_session_factory),
        user=user,
        organization=organization,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, store: StoreFixture) -> None:
    """The same list the in-memory store passes. Neither gets a shorter exam."""
    await check(store)


# -- the service, end to end -------------------------------------------------


async def test_the_full_rotation_cycle_against_postgres(store: StoreFixture) -> None:
    """The one place the whole flow runs against the schema that ships."""
    auth = build_auth(hasher=HASHER, store=store.store, user=store.user)
    auth.user.password_hash = HASHER.hash(PASSWORD)

    first = await auth.service.login(auth.credentials(), context=CONTEXT)
    second = await auth.service.refresh(first.refresh_token, context=CONTEXT)
    assert await auth.service.identify(second.access_token)

    with pytest.raises(SessionExpired):
        await auth.service.refresh(first.refresh_token, context=CONTEXT)

    with pytest.raises(SessionExpired):
        await auth.service.identify(second.access_token)


# -- schema ------------------------------------------------------------------


async def test_email_uniqueness_is_case_insensitive(db_session: AsyncSession) -> None:
    """CITEXT, not application-side lower-casing: no insert path can bypass a column."""
    organization = make_organization()
    db_session.add(organization)
    db_session.add(make_user(hasher=HASHER, email="Ada@Example.com", organization=organization))
    await db_session.flush()

    db_session.add(make_user(hasher=HASHER, email="ada@example.com", organization=organization))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_unknown_role_is_refused(db_session: AsyncSession) -> None:
    organization = make_organization()
    db_session.add(organization)
    db_session.add(make_user(hasher=HASHER, role="wizard", organization=organization))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("role", ["superadmin", "org_admin", "org_member", "org_viewer"])
async def test_every_spec_role_is_accepted(db_session: AsyncSession, role: str) -> None:
    """Task 04 enforces these; the column already has to hold them, or that task starts
    with a migration on a populated table."""
    organization = make_organization()
    db_session.add(organization)
    db_session.add(
        make_user(
            hasher=HASHER,
            email=f"{role}@example.com",
            role=role,
            organization=None if role == "superadmin" else organization,
        )
    )

    await db_session.flush()


async def test_an_unknown_status_is_refused(db_session: AsyncSession) -> None:
    organization = make_organization()
    db_session.add(organization)
    db_session.add(make_user(hasher=HASHER, status="pending", organization=organization))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_superadmin_may_not_belong_to_an_organization(db_session: AsyncSession) -> None:
    organization = make_organization()
    db_session.add(organization)
    db_session.add(make_user(hasher=HASHER, role="superadmin", organization=organization))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_org_user_must_belong_to_one(db_session: AsyncSession) -> None:
    db_session.add(make_user(hasher=HASHER, role="org_admin", organization=None))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_refresh_token_hash_is_unique(db_session: AsyncSession) -> None:
    """Two sessions sharing a hash would make rotation ambiguous."""
    from tests.auth_store_contract import make_session

    organization = make_organization()
    user = make_user(hasher=HASHER, organization=organization)
    db_session.add_all([organization, user])
    await db_session.flush()

    shared = "a" * 64
    db_session.add(make_session(user, token_hash=shared))
    await db_session.flush()
    db_session.add(make_session(user, token_hash=shared))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_a_user_deletes_their_sessions(db_session: AsyncSession) -> None:
    from tests.auth_store_contract import make_session

    organization = make_organization()
    user = make_user(hasher=HASHER, organization=organization)
    db_session.add_all([organization, user])
    await db_session.flush()
    db_session.add(make_session(user))
    await db_session.flush()

    await db_session.delete(user)
    await db_session.flush()

    remaining = (
        (await db_session.execute(select(UserSession).where(UserSession.user_id == user.id)))
        .scalars()
        .all()
    )
    assert remaining == []


async def test_an_ip_column_rejects_nonsense(db_session: AsyncSession) -> None:
    """INET rather than text, so a session list can be queried by subnet during an
    incident — which also means the column validates."""
    from tests.auth_store_contract import make_session

    organization = make_organization()
    user = make_user(hasher=HASHER, organization=organization)
    db_session.add_all([organization, user])
    await db_session.flush()

    record = make_session(user)
    record.ip = "not-an-address"
    db_session.add(record)

    with pytest.raises(DBAPIError):
        await db_session.flush()


# -- seeding -----------------------------------------------------------------


def seed_options(**overrides: object) -> SeedOptions:
    options = SeedOptions(
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        credential="sk-test-credential",
        auth_type="bearer",
        rotate_key=False,
    )
    for name, value in overrides.items():
        setattr(options, name, value)
    return options


async def test_seed_creates_a_superadmin(db_session: AsyncSession, secret_box: SecretBox) -> None:
    result = await seed_demo(db_session, seed_options(), secret_box, HASHER)

    assert result.admin_password is not None
    admin = (
        await db_session.execute(select(User).where(User.email == DEFAULT_ADMIN_EMAIL))
    ).scalar_one()
    assert admin.role == "superadmin"
    assert admin.organization_id is None
    assert HASHER.verify(admin.password_hash or "", result.admin_password)


async def test_the_seeded_password_is_not_stored_in_the_clear(
    db_session: AsyncSession, secret_box: SecretBox
) -> None:
    result = await seed_demo(db_session, seed_options(), secret_box, HASHER)

    admin = (
        await db_session.execute(select(User).where(User.email == DEFAULT_ADMIN_EMAIL))
    ).scalar_one()
    assert result.admin_password is not None
    assert result.admin_password not in (admin.password_hash or "")


async def test_seeding_twice_keeps_the_existing_admin(
    db_session: AsyncSession, secret_box: SecretBox
) -> None:
    """Re-running seed must not silently print a password that is not in the database,
    and must not lock the operator out by changing one they are already using."""
    first = await seed_demo(db_session, seed_options(), secret_box, HASHER)
    stored = (
        (await db_session.execute(select(User).where(User.email == DEFAULT_ADMIN_EMAIL)))
        .scalar_one()
        .password_hash
    )

    second = await seed_demo(db_session, seed_options(), secret_box, HASHER)

    assert second.admin_password is None
    assert first.admin_password is not None
    unchanged = (
        await db_session.execute(select(User).where(User.email == DEFAULT_ADMIN_EMAIL))
    ).scalar_one()
    assert unchanged.password_hash == stored


async def test_rotating_the_admin_password_issues_a_new_one(
    db_session: AsyncSession, secret_box: SecretBox
) -> None:
    first = await seed_demo(db_session, seed_options(), secret_box, HASHER)

    second = await seed_demo(
        db_session, seed_options(rotate_admin_password=True), secret_box, HASHER
    )

    assert second.admin_password is not None
    assert second.admin_password != first.admin_password
    admin = (
        await db_session.execute(select(User).where(User.email == DEFAULT_ADMIN_EMAIL))
    ).scalar_one()
    assert HASHER.verify(admin.password_hash or "", second.admin_password)


async def test_the_admin_can_actually_log_in(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    secret_box: SecretBox,
) -> None:
    """The demo for this task, end to end: seed, then sign in with what it printed."""
    result = await seed_demo(db_session, seed_options(), secret_box, HASHER)
    await db_session.flush()
    assert result.admin_password is not None

    auth = build_auth(hasher=HASHER, store=PostgresAuthStore(db_session_factory))
    issued = await auth.service.login(
        auth.credentials(email=DEFAULT_ADMIN_EMAIL, password=result.admin_password),
        context=CONTEXT,
    )

    assert issued.user.role == "superadmin"
    assert issued.organization is None


async def test_seeding_without_a_provider_key_still_creates_the_admin(
    db_session: AsyncSession, secret_box: SecretBox
) -> None:
    """Task 03's demo needs an account to log in with; it does not need an upstream."""
    result = await seed_demo(
        db_session, seed_options(seed_gateway=False, credential=None), secret_box, HASHER
    )

    assert result.admin_password is not None
    assert result.api_key is None
    assert not result.gateway_seeded
    assert (await db_session.execute(select(Organization))).scalars().first() is not None


# -- redis -------------------------------------------------------------------


async def test_the_redis_throttle_store_counts(clients: object) -> None:
    """The policy is tested in ``tests/test_login_throttle.py`` against the in-memory
    store; this checks the two Redis commands behind it actually work."""
    redis = getattr(clients, "redis", None)
    assert redis is not None
    try:
        await redis.ping()
    except Exception as exc:  # no Redis on this machine
        pytest.skip(f"Redis not available: {exc}")

    from app.core.config import get_settings

    settings = get_settings().model_copy(update={"login_max_attempts": 2})
    attempt = Attempt(email=f"{uuid.uuid4()}@example.com", ip="203.0.113.9")
    throttle = LoginThrottle(RedisThrottleStore(redis), settings)

    await throttle.record_failure(attempt)
    await throttle.check(attempt)
    await throttle.record_failure(attempt)

    with pytest.raises(TooManyAttempts):
        await throttle.check(attempt)

    await throttle.record_success(attempt)
    await redis.delete(f"login-throttle:ip:{attempt.ip}")


def test_uuid7_is_used_for_ids() -> None:
    """Sessions are inserted constantly; random v4 keys would fragment the index."""
    assert uuid7().version == 7
