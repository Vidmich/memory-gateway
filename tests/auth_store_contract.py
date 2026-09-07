"""The behaviour every auth store must have, written once.

The in-memory store exists so the rotation rules can be tested without a database. That
is only worth anything if it answers like the database does — so every question the
service asks a store is asked here, and both implementations run the same checks:
``tests/test_auth_store_memory.py`` and the PostgreSQL half of
``tests/test_auth_db.py``.

Not a test module itself (the filename has no ``test_`` prefix); it is the shared body
those two parametrize over.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.ids import uuid7
from app.core.passwords import Hasher
from app.db.models import Organization, User, UserSession
from app.services.auth_store import AuthStore

#: Weak on purpose: these tests are about storage, not about Argon2.
HASHER = Hasher(time_cost=1, memory_cost_kib=8, parallelism=1)


@dataclass
class StoreFixture:
    store: AuthStore
    user: User
    organization: Organization


def make_session(
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    token_hash: str | None = None,
    expires_in: int = 3600,
    persistent: bool = False,
) -> UserSession:
    return UserSession(
        id=uuid7(),
        user_id=user.id,
        family_id=family_id or uuid7(),
        refresh_token_hash=token_hash or (uuid.uuid4().hex + uuid.uuid4().hex),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        persistent=persistent,
        replaced_at=None,
        revoked_at=None,
        revoked_reason=None,
        ip="203.0.113.7",
        user_agent="pytest",
    )


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


async def a_user_is_found_by_email(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        found = await transaction.user_by_email(fixture.user.email)

    assert found is not None and found.id == fixture.user.id


async def the_email_lookup_is_case_insensitive(fixture: StoreFixture) -> None:
    """CITEXT in PostgreSQL, ``casefold`` in memory. If these ever disagree, a user can
    log in against one and not the other."""
    async with fixture.store.begin() as transaction:
        found = await transaction.user_by_email(fixture.user.email.upper())

    assert found is not None and found.id == fixture.user.id


async def surrounding_whitespace_is_ignored(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        found = await transaction.user_by_email(f"  {fixture.user.email}  ")

    assert found is not None


async def an_unknown_email_returns_nothing(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        assert await transaction.user_by_email("nobody@example.com") is None


async def a_user_is_found_by_id(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        found = await transaction.user_by_id(fixture.user.id)

    assert found is not None and found.email == fixture.user.email


async def an_unknown_id_returns_nothing(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        assert await transaction.user_by_id(uuid7()) is None


async def an_organization_is_found_by_id(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        found = await transaction.organization(fixture.organization.id)

    assert found is not None and found.slug == fixture.organization.slug


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


async def a_session_round_trips(fixture: StoreFixture) -> None:
    record = make_session(fixture.user, persistent=True)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(record)
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        found = await transaction.session_by_token_hash(record.refresh_token_hash)

    assert found is not None
    assert found.family_id == record.family_id
    assert found.persistent is True


async def an_unknown_token_hash_returns_nothing(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        assert await transaction.session_by_token_hash("0" * 64) is None


async def a_new_family_is_live(fixture: StoreFixture) -> None:
    record = make_session(fixture.user)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(record)
        await transaction.commit()
        assert await transaction.family_is_live(record.family_id)


async def an_unknown_family_is_not_live(fixture: StoreFixture) -> None:
    async with fixture.store.begin() as transaction:
        assert not await transaction.family_is_live(uuid7())


async def revoking_a_family_covers_every_token_in_it(fixture: StoreFixture) -> None:
    family = uuid7()
    first = make_session(fixture.user, family_id=family)
    second = make_session(fixture.user, family_id=family)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(first)
        await transaction.add_session(second)
        await transaction.revoke_family(family, reason="reuse_detected", at=datetime.now(UTC))
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        assert not await transaction.family_is_live(family)
        found = await transaction.session_by_token_hash(second.refresh_token_hash)
        assert found is not None
        assert found.revoked_at is not None
        assert found.revoked_reason == "reuse_detected"


async def revoking_one_family_leaves_another_alone(fixture: StoreFixture) -> None:
    doomed = make_session(fixture.user)
    survivor = make_session(fixture.user)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(doomed)
        await transaction.add_session(survivor)
        await transaction.revoke_family(doomed.family_id, reason="logout", at=datetime.now(UTC))
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        assert await transaction.family_is_live(survivor.family_id)


async def revoking_twice_keeps_the_first_reason(fixture: StoreFixture) -> None:
    """Reuse detection is the interesting reason to find in this table later; a
    subsequent logout must not overwrite it."""
    record = make_session(fixture.user)
    moment = datetime.now(UTC)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(record)
        await transaction.revoke_family(record.family_id, reason="reuse_detected", at=moment)
        await transaction.revoke_family(
            record.family_id, reason="logout", at=moment + timedelta(seconds=1)
        )
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        found = await transaction.session_by_token_hash(record.refresh_token_hash)

    assert found is not None and found.revoked_reason == "reuse_detected"


async def revoking_other_families_spares_the_named_one(fixture: StoreFixture) -> None:
    keep = make_session(fixture.user)
    other = make_session(fixture.user)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(keep)
        await transaction.add_session(other)
        await transaction.revoke_other_families(
            user_id=fixture.user.id,
            keep_family_id=keep.family_id,
            reason="password_change",
            at=datetime.now(UTC),
        )
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        assert await transaction.family_is_live(keep.family_id)
        assert not await transaction.family_is_live(other.family_id)


async def revoking_other_families_does_not_touch_another_user(fixture: StoreFixture) -> None:
    """The one place a missing ``user_id`` filter would sign the whole platform out."""
    theirs = make_session(fixture.user)

    async with fixture.store.begin() as transaction:
        await transaction.add_session(theirs)
        await transaction.revoke_other_families(
            user_id=uuid7(),  # somebody else changing their password
            keep_family_id=uuid7(),
            reason="password_change",
            at=datetime.now(UTC),
        )
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        assert await transaction.family_is_live(theirs.family_id)


async def mutating_a_returned_session_persists(fixture: StoreFixture) -> None:
    """Rotation marks the old row replaced by assigning to it; that has to stick."""
    record = make_session(fixture.user)
    async with fixture.store.begin() as transaction:
        await transaction.add_session(record)
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        found = await transaction.session_by_token_hash(record.refresh_token_hash)
        assert found is not None
        found.replaced_at = datetime.now(UTC)
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        again = await transaction.session_by_token_hash(record.refresh_token_hash)

    assert again is not None and again.replaced_at is not None


async def mutating_a_returned_user_persists(fixture: StoreFixture) -> None:
    """The password-hash upgrade on login works the same way."""
    async with fixture.store.begin() as transaction:
        found = await transaction.user_by_id(fixture.user.id)
        assert found is not None
        found.last_login_at = datetime.now(UTC)
        await transaction.commit()

    async with fixture.store.begin() as transaction:
        again = await transaction.user_by_id(fixture.user.id)

    assert again is not None and again.last_login_at is not None


Check = Callable[[StoreFixture], Awaitable[None]]

#: Every check, in one list, so neither implementation can be given a shorter exam.
CHECKS: tuple[Check, ...] = (
    a_user_is_found_by_email,
    the_email_lookup_is_case_insensitive,
    surrounding_whitespace_is_ignored,
    an_unknown_email_returns_nothing,
    a_user_is_found_by_id,
    an_unknown_id_returns_nothing,
    an_organization_is_found_by_id,
    a_session_round_trips,
    an_unknown_token_hash_returns_nothing,
    a_new_family_is_live,
    an_unknown_family_is_not_live,
    revoking_a_family_covers_every_token_in_it,
    revoking_one_family_leaves_another_alone,
    revoking_twice_keeps_the_first_reason,
    revoking_other_families_spares_the_named_one,
    revoking_other_families_does_not_touch_another_user,
    mutating_a_returned_session_persists,
    mutating_a_returned_user_persists,
)
