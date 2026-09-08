"""Envelope encryption for provider credentials.

Each secret gets its own random AES-256-GCM **data key**; that data key is then wrapped
with the master key from ``ENCRYPTION_MASTER_KEY``. The stored blob carries the wrapped
key, so rotating the master key (task 18) means re-wrapping a 32-byte key per row rather
than decrypting and re-encrypting every credential.

The blob is versioned and self-describing:

``` text
version(1) | wrap_nonce(12) | wrapped_data_key(48) | payload_nonce(12) | ciphertext+tag
```

Both layers authenticate a constant AAD, so a blob cannot be replayed into a different
field or a different application without failing to decrypt.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings

VERSION = 1
_AAD = b"memory-gateway/credential/v1"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_WRAPPED_KEY_BYTES = _KEY_BYTES + 16  # AES-GCM appends a 16-byte tag
_HEADER_BYTES = 1 + _NONCE_BYTES + _WRAPPED_KEY_BYTES + _NONCE_BYTES


class DecryptionError(Exception):
    """The blob is malformed, truncated, tampered with, or from another master key."""


@dataclass(frozen=True)
class SecretBox:
    """Encrypts and decrypts secrets under one master key."""

    master_key: bytes

    def __post_init__(self) -> None:
        if len(self.master_key) != _KEY_BYTES:
            raise ValueError(f"master key must be {_KEY_BYTES} bytes, got {len(self.master_key)}")

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SecretBox:
        settings = settings or get_settings()
        return cls(master_key=base64.b64decode(settings.encryption_master_key, validate=True))

    def encrypt(self, plaintext: str) -> bytes:
        data_key = os.urandom(_KEY_BYTES)
        wrap_nonce = os.urandom(_NONCE_BYTES)
        payload_nonce = os.urandom(_NONCE_BYTES)

        wrapped = AESGCM(self.master_key).encrypt(wrap_nonce, data_key, _AAD)
        ciphertext = AESGCM(data_key).encrypt(payload_nonce, plaintext.encode("utf-8"), _AAD)

        return bytes([VERSION]) + wrap_nonce + wrapped + payload_nonce + ciphertext

    def decrypt(self, blob: bytes) -> str:
        if len(blob) < _HEADER_BYTES:
            raise DecryptionError("ciphertext is truncated")
        if blob[0] != VERSION:
            raise DecryptionError(f"unsupported ciphertext version {blob[0]}")

        offset = 1
        wrap_nonce = blob[offset : offset + _NONCE_BYTES]
        offset += _NONCE_BYTES
        wrapped = blob[offset : offset + _WRAPPED_KEY_BYTES]
        offset += _WRAPPED_KEY_BYTES
        payload_nonce = blob[offset : offset + _NONCE_BYTES]
        offset += _NONCE_BYTES
        ciphertext = blob[offset:]

        try:
            data_key = AESGCM(self.master_key).decrypt(wrap_nonce, wrapped, _AAD)
            plaintext = AESGCM(data_key).decrypt(payload_nonce, ciphertext, _AAD)
        except InvalidTag as exc:
            # Deliberately vague: distinguishing "wrong key" from "tampered" tells an
            # attacker which half of the envelope they managed to influence.
            raise DecryptionError("could not decrypt credential") from exc
        return plaintext.decode("utf-8")


def rewrap(blob: bytes, *, old: SecretBox, new: SecretBox) -> bytes:
    """Re-encrypt a credential's data key under a new master key, leaving the payload alone.

    This is what the envelope was for. Rotating ``ENCRYPTION_MASTER_KEY`` touches 48 bytes
    per row rather than the credential itself, so the plaintext is never held longer than
    the microseconds it takes to unwrap a key — and never at all, in fact: the payload
    ciphertext and its nonce are copied across byte for byte.

    Both layers authenticate the same constant AAD, so a blob that has been through this
    is indistinguishable from one written by ``encrypt`` under the new key. That matters
    more than it sounds: it means rotation leaves no second format to support, and a
    half-finished rotation is a table with rows under two master keys rather than rows in
    two shapes.
    """
    if len(blob) < _HEADER_BYTES:
        raise DecryptionError("ciphertext is truncated")
    if blob[0] != VERSION:
        raise DecryptionError(f"unsupported ciphertext version {blob[0]}")

    wrap_nonce = blob[1 : 1 + _NONCE_BYTES]
    wrapped = blob[1 + _NONCE_BYTES : 1 + _NONCE_BYTES + _WRAPPED_KEY_BYTES]
    remainder = blob[1 + _NONCE_BYTES + _WRAPPED_KEY_BYTES :]

    try:
        data_key = AESGCM(old.master_key).decrypt(wrap_nonce, wrapped, _AAD)
    except InvalidTag as exc:
        raise DecryptionError("could not decrypt credential") from exc

    # A fresh nonce for the new wrap. Reusing the old one under a different key would be
    # safe in AES-GCM's terms and is still the wrong habit to write down.
    fresh_nonce = os.urandom(_NONCE_BYTES)
    rewrapped = AESGCM(new.master_key).encrypt(fresh_nonce, data_key, _AAD)
    return bytes([VERSION]) + fresh_nonce + rewrapped + remainder


def secret_hint(plaintext: str) -> str:
    """A non-reversible display form, per SPEC §5.4: ``sk-...4f2a``.

    Secrets are write-only over the API; this is the only thing a response may carry.
    Anything short enough that the hint would reveal most of it is masked entirely.
    """
    if len(plaintext) < 12:
        return "..."
    return f"{plaintext[:3]}...{plaintext[-4:]}"
