"""Login, rotation, reuse detection, logout, and password change.

The rotation rules are the security-critical part of task 03, so they are tested against
an in-memory store and run everywhere. ``tests/test_auth_db.py`` proves the PostgreSQL
store answers the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core import tokens
from app.core.passwords import Hasher
from app.core.tokens import decode_access_token
from app.services.auth import (
    AuthenticationRequired,
    InvalidCredentials,
    LoginThrottled,
    SessionExpired,
    WeakPassword,
)
from tests.auth_support import CONTEXT, EMAIL, PASSWORD, AuthFixture, build_auth, make_user


@pytest.fixture
def auth() -> AuthFixture:
    return build_auth()


# -- login -------------------------------------------------------------------


async def test_login_returns_a_usable_access_token(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    claims = decode_access_token(issued.access_token, auth.settings)
    assert claims.user_id == auth.user.id
    assert claims.role == auth.user.role
    assert claims.organization_id == auth.user.organization_id


async def test_login_records_the_session(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    records = auth.sessions_of(issued.family_id)
    assert len(records) == 1
    assert records[0].refresh_token_hash == tokens.hash_refresh_token(issued.refresh_token)
    assert records[0].ip == CONTEXT.ip
    assert records[0].user_agent == CONTEXT.user_agent


async def test_login_stamps_last_login(auth: AuthFixture) -> None:
    await auth.service.login(auth.credentials(), context=CONTEXT)

    assert auth.user.last_login_at is not None


async def test_login_returns_the_organization(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    assert issued.organization is not None
    assert issued.organization.id == auth.user.organization_id


async def test_a_superadmin_logs_in_without_an_organization() -> None:
    auth = build_auth(with_organization=False)
    auth.user.role = "superadmin"

    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    assert issued.organization is None
    assert decode_access_token(issued.access_token, auth.settings).organization_id is None


async def test_login_is_case_insensitive_on_email(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(email=EMAIL.upper()), context=CONTEXT)

    assert issued.user.id == auth.user.id


async def test_the_wrong_password_is_refused(auth: AuthFixture) -> None:
    with pytest.raises(InvalidCredentials):
        await auth.service.login(auth.credentials(password="wrong-password"), context=CONTEXT)


async def test_an_unknown_email_gives_the_same_message_as_a_wrong_password(
    auth: AuthFixture,
) -> None:
    """Two different messages here is an account-enumeration endpoint."""
    with pytest.raises(InvalidCredentials) as unknown:
        await auth.service.login(auth.credentials(email="nobody@example.com"), context=CONTEXT)
    with pytest.raises(InvalidCredentials) as wrong:
        await auth.service.login(auth.credentials(password="wrong-password"), context=CONTEXT)

    assert unknown.value.message == wrong.value.message


async def test_a_suspended_user_cannot_log_in() -> None:
    auth = build_auth()
    auth.user.status = "suspended"

    with pytest.raises(InvalidCredentials):
        await auth.service.login(auth.credentials(), context=CONTEXT)


async def test_an_invited_user_without_a_password_cannot_log_in() -> None:
    hasher = Hasher(time_cost=1, memory_cost_kib=8, parallelism=1)
    auth = build_auth(hasher=hasher, user=make_user(hasher=hasher, password=None, status="invited"))

    with pytest.raises(InvalidCredentials):
        await auth.service.login(auth.credentials(), context=CONTEXT)


async def test_remember_me_lengthens_the_refresh_token(auth: AuthFixture) -> None:
    short = await auth.service.login(auth.credentials(), context=CONTEXT, remember=False)
    long = await auth.service.login(auth.credentials(), context=CONTEXT, remember=True)

    assert long.refresh_expires_at > short.refresh_expires_at
    assert long.persistent and not short.persistent


async def test_a_stale_password_hash_is_upgraded_on_login() -> None:
    weak = Hasher(time_cost=1, memory_cost_kib=8, parallelism=1)
    strong = Hasher(time_cost=2, memory_cost_kib=16, parallelism=1)
    auth = build_auth(hasher=strong, user=make_user(hasher=weak))
    stored_before = auth.user.password_hash

    await auth.service.login(auth.credentials(), context=CONTEXT)

    assert auth.user.password_hash != stored_before
    assert strong.verify(auth.user.password_hash or "", PASSWORD)


async def test_repeated_failures_are_throttled(auth: AuthFixture) -> None:
    for _ in range(auth.settings.login_max_attempts):
        with pytest.raises(InvalidCredentials):
            await auth.service.login(auth.credentials(password="wrong-password"), context=CONTEXT)

    with pytest.raises(LoginThrottled) as caught:
        await auth.service.login(auth.credentials(), context=CONTEXT)

    assert caught.value.status_code == 429
    assert "retry-after" in caught.value.headers


async def test_the_throttle_refuses_before_checking_the_password(auth: AuthFixture) -> None:
    """A locked-out attacker must not be able to use response timing as an oracle: the
    correct password is refused exactly like a wrong one."""
    for _ in range(auth.settings.login_max_attempts):
        with pytest.raises(InvalidCredentials):
            await auth.service.login(auth.credentials(password="wrong-password"), context=CONTEXT)

    with pytest.raises(LoginThrottled):
        await auth.service.login(auth.credentials(), context=CONTEXT)


# -- refresh -----------------------------------------------------------------


async def test_refresh_issues_a_new_pair(auth: AuthFixture) -> None:
    first = await auth.service.login(auth.credentials(), context=CONTEXT)

    second = await auth.service.refresh(first.refresh_token, context=CONTEXT)

    assert second.refresh_token != first.refresh_token
    assert second.access_token != first.access_token


async def test_refresh_stays_in_the_same_family(auth: AuthFixture) -> None:
    """Rotating a refresh token must not invalidate an access token the client is still
    legitimately holding — so the session id in the JWT is the family, not the row."""
    first = await auth.service.login(auth.credentials(), context=CONTEXT)

    second = await auth.service.refresh(first.refresh_token, context=CONTEXT)

    assert second.family_id == first.family_id
    assert len(auth.sessions_of(first.family_id)) == 2


async def test_the_old_token_is_marked_replaced(auth: AuthFixture) -> None:
    first = await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.refresh(first.refresh_token, context=CONTEXT)

    old = next(
        record
        for record in auth.sessions_of(first.family_id)
        if record.refresh_token_hash == tokens.hash_refresh_token(first.refresh_token)
    )
    assert old.replaced_at is not None


async def test_refresh_carries_the_remember_me_choice(auth: AuthFixture) -> None:
    """A session the user asked not to persist must not become a persistent one by
    virtue of staying open long enough to refresh."""
    first = await auth.service.login(auth.credentials(), context=CONTEXT, remember=False)

    second = await auth.service.refresh(first.refresh_token, context=CONTEXT)

    assert not second.persistent


async def test_an_unknown_refresh_token_is_refused(auth: AuthFixture) -> None:
    with pytest.raises(SessionExpired):
        await auth.service.refresh("not-a-real-token", context=CONTEXT)


async def test_an_expired_refresh_token_is_refused(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)
    auth.expire(tokens.hash_refresh_token(issued.refresh_token))

    with pytest.raises(SessionExpired):
        await auth.service.refresh(issued.refresh_token, context=CONTEXT)

    assert all(record.revoked_at is not None for record in auth.sessions_of(issued.family_id))


async def test_a_suspended_user_cannot_refresh(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)
    auth.user.status = "suspended"

    with pytest.raises(SessionExpired):
        await auth.service.refresh(issued.refresh_token, context=CONTEXT)


# -- reuse detection ---------------------------------------------------------


async def test_replaying_a_spent_token_is_refused(auth: AuthFixture) -> None:
    first = await auth.service.login(auth.credentials(), context=CONTEXT)
    await auth.service.refresh(first.refresh_token, context=CONTEXT)

    with pytest.raises(SessionExpired):
        await auth.service.refresh(first.refresh_token, context=CONTEXT)


async def test_replaying_a_spent_token_kills_the_whole_family(auth: AuthFixture) -> None:
    """The attacker and the victim both hold something that works; there is no way to
    tell them apart, so neither keeps the session."""
    first = await auth.service.login(auth.credentials(), context=CONTEXT)
    second = await auth.service.refresh(first.refresh_token, context=CONTEXT)

    with pytest.raises(SessionExpired):
        await auth.service.refresh(first.refresh_token, context=CONTEXT)

    with pytest.raises(SessionExpired):
        await auth.service.refresh(second.refresh_token, context=CONTEXT)
    assert {record.revoked_reason for record in auth.sessions_of(first.family_id)} == {
        "reuse_detected"
    }


async def test_reuse_detection_does_not_touch_other_sessions(auth: AuthFixture) -> None:
    """Signing out of a laptop's stolen session must not sign the phone out too."""
    laptop = await auth.service.login(auth.credentials(), context=CONTEXT)
    phone = await auth.service.login(auth.credentials(), context=CONTEXT)
    await auth.service.refresh(laptop.refresh_token, context=CONTEXT)

    with pytest.raises(SessionExpired):
        await auth.service.refresh(laptop.refresh_token, context=CONTEXT)

    assert await auth.service.refresh(phone.refresh_token, context=CONTEXT)


async def test_a_revoked_family_cannot_be_refreshed(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)
    await auth.service.logout(issued.refresh_token)

    with pytest.raises(SessionExpired):
        await auth.service.refresh(issued.refresh_token, context=CONTEXT)


# -- logout ------------------------------------------------------------------


async def test_logout_revokes_the_family(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)
    await auth.service.refresh(issued.refresh_token, context=CONTEXT)

    await auth.service.logout(issued.refresh_token)

    assert all(record.revoked_at is not None for record in auth.sessions_of(issued.family_id))


async def test_logout_invalidates_the_access_token_immediately(auth: AuthFixture) -> None:
    """The whole reason `identify` pays for a lookup instead of trusting the JWT."""
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.logout(issued.refresh_token)

    with pytest.raises(SessionExpired):
        await auth.service.identify(issued.access_token)


async def test_logging_out_twice_is_not_an_error(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.logout(issued.refresh_token)
    await auth.service.logout(issued.refresh_token)


async def test_logging_out_with_no_token_is_not_an_error(auth: AuthFixture) -> None:
    await auth.service.logout(None)
    await auth.service.logout("")


async def test_logging_out_with_an_unknown_token_is_not_an_error(auth: AuthFixture) -> None:
    await auth.service.logout("not-a-real-token")


# -- identify ----------------------------------------------------------------


async def test_identify_returns_the_user_and_organization(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    identity = await auth.service.identify(issued.access_token)

    assert identity.user.id == auth.user.id
    assert identity.organization is not None
    assert identity.family_id == issued.family_id


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c"])
async def test_identify_refuses_a_bad_token(auth: AuthFixture, token: str) -> None:
    with pytest.raises(AuthenticationRequired):
        await auth.service.identify(token)


async def test_identify_refuses_an_expired_token(auth: AuthFixture) -> None:
    expired, _ = tokens.issue_access_token(
        user_id=auth.user.id,
        organization_id=auth.user.organization_id,
        role=auth.user.role,
        session_id=auth.user.id,
        settings=auth.settings,
        now=datetime.now(UTC) - timedelta(days=1),
    )

    with pytest.raises(AuthenticationRequired):
        await auth.service.identify(expired)


async def test_identify_refuses_a_suspended_user(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)
    auth.user.status = "suspended"

    with pytest.raises(AuthenticationRequired):
        await auth.service.identify(issued.access_token)


async def test_identify_refuses_a_token_for_a_session_that_never_existed(
    auth: AuthFixture,
) -> None:
    """A validly signed token is not enough; the session behind it has to be live."""
    orphan, _ = tokens.issue_access_token(
        user_id=auth.user.id,
        organization_id=auth.user.organization_id,
        role=auth.user.role,
        session_id=auth.user.id,  # no session family has this id
        settings=auth.settings,
    )

    with pytest.raises(SessionExpired):
        await auth.service.identify(orphan)


# -- password change ---------------------------------------------------------

NEW_PASSWORD = "a-brand-new-password"


async def test_changing_the_password_works(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.change_password(
        auth.user.id,
        current_password=PASSWORD,
        new_password=NEW_PASSWORD,
        keep_family_id=issued.family_id,
    )

    assert await auth.service.login(auth.credentials(password=NEW_PASSWORD), context=CONTEXT)


async def test_the_old_password_stops_working(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.change_password(
        auth.user.id,
        current_password=PASSWORD,
        new_password=NEW_PASSWORD,
        keep_family_id=issued.family_id,
    )

    with pytest.raises(InvalidCredentials):
        await auth.service.login(auth.credentials(), context=CONTEXT)


async def test_changing_the_password_needs_the_current_one(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    with pytest.raises(InvalidCredentials):
        await auth.service.change_password(
            auth.user.id,
            current_password="not-the-current-password",
            new_password=NEW_PASSWORD,
            keep_family_id=issued.family_id,
        )


async def test_a_weak_new_password_is_rejected(auth: AuthFixture) -> None:
    issued = await auth.service.login(auth.credentials(), context=CONTEXT)

    with pytest.raises(WeakPassword):
        await auth.service.change_password(
            auth.user.id,
            current_password=PASSWORD,
            new_password="short",
            keep_family_id=issued.family_id,
        )


async def test_changing_the_password_signs_other_sessions_out(auth: AuthFixture) -> None:
    laptop = await auth.service.login(auth.credentials(), context=CONTEXT)
    phone = await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.change_password(
        auth.user.id,
        current_password=PASSWORD,
        new_password=NEW_PASSWORD,
        keep_family_id=laptop.family_id,
    )

    with pytest.raises(SessionExpired):
        await auth.service.refresh(phone.refresh_token, context=CONTEXT)


async def test_changing_the_password_keeps_the_current_session(auth: AuthFixture) -> None:
    laptop = await auth.service.login(auth.credentials(), context=CONTEXT)
    await auth.service.login(auth.credentials(), context=CONTEXT)

    await auth.service.change_password(
        auth.user.id,
        current_password=PASSWORD,
        new_password=NEW_PASSWORD,
        keep_family_id=laptop.family_id,
    )

    assert await auth.service.identify(laptop.access_token)
    assert await auth.service.refresh(laptop.refresh_token, context=CONTEXT)
