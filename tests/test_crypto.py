"""Envelope encryption: it must round-trip, and it must refuse everything else."""

from __future__ import annotations

import base64
import os

import pytest

from app.core.crypto import VERSION, DecryptionError, SecretBox, rewrap, secret_hint


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


# ---------------------------------------------------------------------------
# master-key rotation (task 18)
# ---------------------------------------------------------------------------


def test_rewrapping_keeps_the_plaintext_readable_under_the_new_key() -> None:
    """What the envelope was for. Rotation touches 48 bytes per row rather than the
    credential, so the plaintext is never re-encrypted and never leaves this function."""
    old = SecretBox(master_key=b"o" * 32)
    new = SecretBox(master_key=b"n" * 32)
    blob = old.encrypt("sk-rotate-me")

    rewrapped = rewrap(blob, old=old, new=new)

    assert new.decrypt(rewrapped) == "sk-rotate-me"


def test_the_payload_is_copied_across_byte_for_byte() -> None:
    """The property that makes rotation cheap and makes it safe: only the wrapped data key
    changes, so a rotation over a large table is a fixed cost per row rather than one
    proportional to what the rows hold."""
    old = SecretBox(master_key=b"o" * 32)
    new = SecretBox(master_key=b"n" * 32)
    blob = old.encrypt("sk-rotate-me")

    rewrapped = rewrap(blob, old=old, new=new)

    # version(1) + wrap_nonce(12) + wrapped key(48) is the header; everything after it is
    # the payload nonce and the ciphertext.
    assert rewrapped[61:] == blob[61:]
    assert rewrapped[1:61] != blob[1:61]


def test_a_rewrapped_blob_is_indistinguishable_from_a_freshly_written_one() -> None:
    """Which is what keeps a half-finished rotation from being a second format to support:
    the table ends up with rows under two master keys, not rows in two shapes."""
    old = SecretBox(master_key=b"o" * 32)
    new = SecretBox(master_key=b"n" * 32)

    rewrapped = rewrap(old.encrypt("sk-value"), old=old, new=new)
    fresh = new.encrypt("sk-value")

    assert len(rewrapped) == len(fresh)
    assert rewrapped[0] == fresh[0] == VERSION


def test_the_old_key_can_no_longer_read_it() -> None:
    old = SecretBox(master_key=b"o" * 32)
    new = SecretBox(master_key=b"n" * 32)

    rewrapped = rewrap(old.encrypt("sk-value"), old=old, new=new)

    with pytest.raises(DecryptionError):
        old.decrypt(rewrapped)


def test_rewrapping_with_the_wrong_previous_key_is_refused() -> None:
    """The rotation command relies on this to tell "already rotated" from "corrupt": it
    tries the current key first and only re-wraps what that cannot read."""
    old = SecretBox(master_key=b"o" * 32)
    wrong = SecretBox(master_key=b"w" * 32)
    new = SecretBox(master_key=b"n" * 32)

    with pytest.raises(DecryptionError):
        rewrap(old.encrypt("sk-value"), old=wrong, new=new)


def test_a_truncated_blob_is_refused_rather_than_producing_a_shorter_one() -> None:
    old = SecretBox(master_key=b"o" * 32)
    new = SecretBox(master_key=b"n" * 32)

    with pytest.raises(DecryptionError):
        rewrap(old.encrypt("sk-value")[:20], old=old, new=new)
