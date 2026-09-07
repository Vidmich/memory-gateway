"""Envelope encryption: it must round-trip, and it must refuse everything else."""

from __future__ import annotations

import base64
import os

import pytest

from app.core.crypto import DecryptionError, SecretBox, secret_hint


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(master_key=os.urandom(32))


def test_round_trip(box: SecretBox) -> None:
    assert box.decrypt(box.encrypt("sk-provider-secret")) == "sk-provider-secret"


def test_unicode_survives(box: SecretBox) -> None:
    secret = "clé-très-secrète-🔐"
    assert box.decrypt(box.encrypt(secret)) == secret


def test_plaintext_does_not_appear_in_the_blob(box: SecretBox) -> None:
    assert b"sk-provider-secret" not in box.encrypt("sk-provider-secret")


def test_each_encryption_uses_a_fresh_data_key(box: SecretBox) -> None:
    """Identical plaintexts must not produce identical ciphertexts, or the store leaks
    which models share a credential."""
    assert box.encrypt("same") != box.encrypt("same")


def test_another_master_key_cannot_decrypt(box: SecretBox) -> None:
    other = SecretBox(master_key=os.urandom(32))

    with pytest.raises(DecryptionError):
        other.decrypt(box.encrypt("secret"))


@pytest.mark.parametrize("index", [0, 1, 20, 60, -1])
def test_tampering_is_detected(box: SecretBox, index: int) -> None:
    blob = bytearray(box.encrypt("secret-value-long-enough"))
    blob[index] ^= 0xFF

    with pytest.raises(DecryptionError):
        box.decrypt(bytes(blob))


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x01" + b"\x00" * 40])
def test_truncated_input_is_rejected(box: SecretBox, blob: bytes) -> None:
    with pytest.raises(DecryptionError):
        box.decrypt(blob)


def test_unknown_version_is_rejected(box: SecretBox) -> None:
    blob = bytearray(box.encrypt("secret"))
    blob[0] = 99

    with pytest.raises(DecryptionError, match="version"):
        box.decrypt(bytes(blob))


@pytest.mark.parametrize("size", [16, 31, 33, 64])
def test_master_key_must_be_32_bytes(size: int) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        SecretBox(master_key=os.urandom(size))


def test_from_settings_uses_the_configured_key(settings_master_key: str) -> None:
    box = SecretBox(master_key=base64.b64decode(settings_master_key))

    assert box.decrypt(box.encrypt("x")) == "x"


@pytest.fixture
def settings_master_key() -> str:
    from app.core.config import get_settings

    return get_settings().encryption_master_key


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("sk-abcdefghijklmnop4f2a", "sk-...4f2a"),
        ("short", "..."),
        ("", "..."),
    ],
)
def test_hint_reveals_only_the_edges(secret: str, expected: str) -> None:
    assert secret_hint(secret) == expected
