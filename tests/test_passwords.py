"""Argon2id hashing, verification, and the rehash-on-login upgrade path."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from app.core.config import Settings, get_settings
from app.core.passwords import (
    MIN_PASSWORD_LENGTH,
    Hasher,
    PasswordPolicy,
    build_hasher,
)

WEAK = {"time_cost": 1, "memory_cost_kib": 8, "parallelism": 1}
STRONGER = {"time_cost": 2, "memory_cost_kib": 16, "parallelism": 1}


@pytest.fixture
def hasher() -> Hasher:
    return Hasher(**WEAK)


def test_a_hash_is_not_the_password(hasher: Hasher) -> None:
    stored = hasher.hash("hunter2hunter2")

    assert "hunter2hunter2" not in stored
    assert stored.startswith("$argon2id$")


def test_the_same_password_hashes_differently_each_time(hasher: Hasher) -> None:
    """A per-hash salt is what stops one rainbow table from covering every user."""
    assert hasher.hash("hunter2hunter2") != hasher.hash("hunter2hunter2")


def test_the_right_password_verifies(hasher: Hasher) -> None:
    assert hasher.verify(hasher.hash("hunter2hunter2"), "hunter2hunter2")


def test_the_wrong_password_does_not(hasher: Hasher) -> None:
    assert not hasher.verify(hasher.hash("hunter2hunter2"), "hunter3hunter3")


@pytest.mark.parametrize(
    "stored",
    ["", "not-a-hash", "$argon2id$v=19$m=8,t=1,p=1$aaa", "$2b$12$abcdefghijklmnopqrstuv"],
    ids=["empty", "garbage", "truncated-argon2", "bcrypt"],
)
def test_a_corrupt_stored_hash_is_a_failed_login_not_a_crash(hasher: Hasher, stored: str) -> None:
    """One bad row must not be able to take the login endpoint down."""
    assert not hasher.verify(stored, "hunter2hunter2")


def test_an_unchanged_cost_needs_no_rehash(hasher: Hasher) -> None:
    ok, upgraded = hasher.verify_and_upgrade(hasher.hash("hunter2hunter2"), "hunter2hunter2")

    assert ok
    assert upgraded is None


def test_a_raised_cost_rehashes_on_login() -> None:
    """The parameters live in the stored hash, so raising them is a config change plus a
    transparent upgrade — not a migration and a forced password reset."""
    old = Hasher(**WEAK)
    new = Hasher(**STRONGER)
    stored = old.hash("hunter2hunter2")

    ok, upgraded = new.verify_and_upgrade(stored, "hunter2hunter2")

    assert ok
    assert upgraded is not None and upgraded != stored
    assert new.verify(upgraded, "hunter2hunter2")
    assert _argon2(STRONGER).check_needs_rehash(upgraded) is False


def test_a_wrong_password_is_never_rehashed() -> None:
    """Otherwise the upgrade path would be a way to overwrite someone's password."""
    stored = Hasher(**WEAK).hash("right-password")

    ok, upgraded = Hasher(**STRONGER).verify_and_upgrade(stored, "wrong")

    assert not ok
    assert upgraded is None


def test_the_lower_cost_hasher_still_accepts_a_stronger_hash() -> None:
    """Lowering the cost must not lock everyone out of their accounts."""
    stored = Hasher(**STRONGER).hash("hunter2hunter2")

    assert Hasher(**WEAK).verify(stored, "hunter2hunter2")


# -- policy ------------------------------------------------------------------


def test_a_long_enough_password_is_accepted() -> None:
    assert PasswordPolicy().check("a" * MIN_PASSWORD_LENGTH) is None


def test_a_short_password_is_rejected_with_a_reason() -> None:
    reason = PasswordPolicy().check("a" * (MIN_PASSWORD_LENGTH - 1))

    assert reason is not None
    assert str(MIN_PASSWORD_LENGTH) in reason


def test_an_enormous_password_is_rejected() -> None:
    """Argon2id would refuse it anyway; refusing first keeps a megabyte of work out of
    the request path."""
    assert PasswordPolicy().check("a" * 5000) is not None


# -- configuration -----------------------------------------------------------


def test_the_shipped_defaults_are_not_the_test_defaults() -> None:
    """The suite runs with a deliberately weak cost (see pyproject). This asserts the
    value a deployment gets when it sets nothing, which is the one that matters."""
    defaults = Settings.model_fields

    assert defaults["password_memory_cost_kib"].default == 65536
    assert defaults["password_time_cost"].default == 3


def test_build_hasher_reads_the_settings() -> None:
    settings = get_settings()
    built = build_hasher(settings)

    stored = built.hash("hunter2hunter2")

    assert f"m={settings.password_memory_cost_kib}" in stored
    assert f"t={settings.password_time_cost}" in stored


def _argon2(params: dict[str, int]) -> PasswordHasher:
    """The same parameters `Hasher` uses, for asserting on rehash decisions directly."""
    return PasswordHasher(
        time_cost=params["time_cost"],
        memory_cost=params["memory_cost_kib"],
        parallelism=params["parallelism"],
        hash_len=32,
        salt_len=16,
    )
