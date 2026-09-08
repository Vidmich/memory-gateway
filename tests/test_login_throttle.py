"""Login backoff policy.

Run against :class:`MemoryThrottleStore`. The Redis store is the same two commands and is
exercised by ``tests/test_auth_db.py`` when a Redis is reachable; the interesting part —
which counters exist, when they trip, and what a success clears — is here.
"""

from __future__ import annotations

import logging

import pytest

from app.core.config import Settings, get_settings
from app.services.login_throttle import (
    Attempt,
    LoginThrottle,
    MemoryThrottleStore,
    TooManyAttempts,
)

ALICE = Attempt(email="alice@example.com", ip="203.0.113.7")


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "login_max_attempts": 3,
            "login_attempt_window_seconds": 900,
            "login_lockout_seconds": 900,
        }
    )


@pytest.fixture
def throttle(settings: Settings) -> LoginThrottle:
    return LoginThrottle(MemoryThrottleStore(), settings)


async def fail(throttle: LoginThrottle, attempt: Attempt, times: int) -> None:
    for _ in range(times):
        await throttle.record_failure(attempt)


async def test_a_fresh_attempt_is_allowed(throttle: LoginThrottle) -> None:
    await throttle.check(ALICE)  # does not raise


async def test_attempts_below_the_limit_are_allowed(
    throttle: LoginThrottle, settings: Settings
) -> None:
    await fail(throttle, ALICE, settings.login_max_attempts - 1)

    await throttle.check(ALICE)


async def test_the_limit_locks_the_account_out(throttle: LoginThrottle, settings: Settings) -> None:
    await fail(throttle, ALICE, settings.login_max_attempts)

    with pytest.raises(TooManyAttempts):
        await throttle.check(ALICE)


async def test_the_lockout_says_how_long_to_wait(
    throttle: LoginThrottle, settings: Settings
) -> None:
    await fail(throttle, ALICE, settings.login_max_attempts)

    with pytest.raises(TooManyAttempts) as caught:
        await throttle.check(ALICE)

    assert 0 < caught.value.retry_after_seconds <= settings.login_attempt_window_seconds


async def test_checking_does_not_itself_count_as_an_attempt(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """`check` runs on every login, including the successful ones."""
    await fail(throttle, ALICE, settings.login_max_attempts - 1)

    for _ in range(10):
        await throttle.check(ALICE)


async def test_a_different_ip_still_trips_the_email_counter(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """The point of counting per email: a distributed attack shows up on no single IP."""
    for index in range(settings.login_max_attempts):
        await throttle.record_failure(Attempt(email=ALICE.email, ip=f"198.51.100.{index}"))

    with pytest.raises(TooManyAttempts):
        await throttle.check(Attempt(email=ALICE.email, ip="198.51.100.200"))


async def test_a_different_email_still_trips_the_ip_counter(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """The point of counting per IP: spraying many accounts trips no single email."""
    for index in range(settings.login_max_attempts):
        await throttle.record_failure(Attempt(email=f"user{index}@example.com", ip=ALICE.ip))

    with pytest.raises(TooManyAttempts):
        await throttle.check(Attempt(email="someone-else@example.com", ip=ALICE.ip))


async def test_an_unrelated_attempt_is_unaffected(
    throttle: LoginThrottle, settings: Settings
) -> None:
    await fail(throttle, ALICE, settings.login_max_attempts)

    await throttle.check(Attempt(email="bob@example.com", ip="198.51.100.1"))


async def test_email_counting_is_case_insensitive(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """The column is case-insensitive, so the counter has to be too — otherwise the
    limit is really `max_attempts` times the number of ways to spell the address."""
    for _ in range(settings.login_max_attempts):
        await throttle.record_failure(Attempt(email="ALICE@example.com", ip=None))

    with pytest.raises(TooManyAttempts):
        await throttle.check(Attempt(email="alice@example.com", ip=None))


async def test_success_clears_the_email_counter(
    throttle: LoginThrottle, settings: Settings
) -> None:
    await fail(throttle, ALICE, settings.login_max_attempts - 1)

    await throttle.record_success(ALICE)
    await fail(throttle, Attempt(email=ALICE.email, ip=None), settings.login_max_attempts - 1)

    await throttle.check(Attempt(email=ALICE.email, ip=None))


async def test_success_does_not_clear_the_ip_counter(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """Otherwise one account the attacker owns resets their budget between guesses at
    everyone else's."""
    for index in range(settings.login_max_attempts):
        await throttle.record_failure(Attempt(email=f"victim{index}@example.com", ip=ALICE.ip))

    await throttle.record_success(Attempt(email="attacker@example.com", ip=ALICE.ip))

    with pytest.raises(TooManyAttempts):
        await throttle.check(Attempt(email="victim99@example.com", ip=ALICE.ip))


async def test_an_attempt_with_neither_email_nor_ip_is_never_throttled(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """There is nothing to count. Refusing outright would deny service to anyone behind
    a proxy that strips the client address."""
    anonymous = Attempt(email=None, ip=None)
    await fail(throttle, anonymous, settings.login_max_attempts * 3)

    await throttle.check(anonymous)


async def test_the_key_space_carries_no_plaintext_addresses(
    throttle: LoginThrottle, settings: Settings
) -> None:
    """This counter is written on every failed login; a store full of addresses would be
    a credential-stuffing list with a TTL."""
    store = MemoryThrottleStore()
    await LoginThrottle(store, settings).record_failure(ALICE)

    keys = list(store._counts)

    assert any("alice@example.com" in key for key in keys) is False
    assert any(key.endswith(ALICE.ip or "") for key in keys)


async def test_the_window_expires(settings: Settings) -> None:
    """A lockout has to end on its own; an operator should not have to clear it."""
    quick = settings.model_copy(update={"login_attempt_window_seconds": 1})
    store = MemoryThrottleStore()
    throttle = LoginThrottle(store, quick)
    await fail(throttle, ALICE, quick.login_max_attempts)

    # Rewind every window instead of sleeping: the test is about expiry, not duration.
    store._counts = {key: (count, expires - 5) for key, (count, expires) in store._counts.items()}

    await throttle.check(ALICE)


# -- when the store is down --------------------------------------------------


class BrokenStore:
    """Every operation fails, the way redis-py behaves with nothing listening."""

    async def increment(self, key: str, ttl_seconds: int) -> int:
        raise ConnectionError("no redis")

    async def peek(self, key: str) -> tuple[int, int]:
        raise ConnectionError("no redis")

    async def delete(self, key: str) -> None:
        raise ConnectionError("no redis")


async def test_a_dead_store_does_not_break_login(settings: Settings) -> None:
    """Fails open. Failing closed would lock every operator out of the UI during a Redis
    outage — which is when they most need to get in — and `/readyz` already pulls an
    instance with no Redis out of the load balancer."""
    throttle = LoginThrottle(BrokenStore(), settings)

    await throttle.check(ALICE)
    await throttle.record_failure(ALICE)
    await throttle.record_success(ALICE)


async def test_a_dead_store_is_logged_loudly(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Silently losing the throttle is how you find out months later."""
    with caplog.at_level(logging.WARNING, logger="app.services.login_throttle"):
        await LoginThrottle(BrokenStore(), settings).check(ALICE)

    assert any("throttle unavailable" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# the unlock path (task 18)
# ---------------------------------------------------------------------------


async def test_unlocking_an_email_lets_the_next_attempt_through(settings: Settings) -> None:
    """The documented way out of a lockout. It matters most during an incident, which is
    exactly when an administrator is both mistyping their password and being attacked."""
    throttle = LoginThrottle(MemoryThrottleStore(), settings)
    attempt = Attempt(email="admin@example.com", ip=None)
    for _ in range(settings.login_max_attempts):
        await throttle.record_failure(attempt)
    with pytest.raises(TooManyAttempts):
        await throttle.check(attempt)

    cleared = await throttle.unlock(attempt)

    assert cleared == 1
    await throttle.check(attempt)


async def test_unlocking_reports_nothing_when_no_counter_was_set(settings: Settings) -> None:
    """An unlock that reports zero means the lockout is somewhere else — a suspended
    organization, say, which refuses login for a different reason with a different
    message. Worth being able to tell apart at 3 a.m."""
    throttle = LoginThrottle(MemoryThrottleStore(), settings)

    assert await throttle.unlock(Attempt(email="nobody@example.com", ip=None)) == 0


async def test_an_address_and_an_email_can_be_cleared_together(settings: Settings) -> None:
    throttle = LoginThrottle(MemoryThrottleStore(), settings)
    attempt = Attempt(email="admin@example.com", ip="203.0.113.9")
    for _ in range(settings.login_max_attempts):
        await throttle.record_failure(attempt)

    assert await throttle.unlock(attempt) == 2
    await throttle.check(attempt)
