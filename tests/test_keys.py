"""API key minting, parsing, and verification."""

from __future__ import annotations

import secrets

import pytest

from app.core import keys
from app.core.ids import uuid7


def test_minted_token_round_trips() -> None:
    key_id = uuid7()
    minted = keys.mint(key_id)

    parsed = keys.parse(minted.token)

    assert parsed is not None
    assert parsed.key_id == key_id
    assert keys.verify(parsed.secret, minted.key_hash)


def test_token_embeds_its_row_id_so_lookup_is_indexed() -> None:
    key_id = uuid7()

    assert keys.mint(key_id).token.startswith(f"mg_{key_id.hex}_")


def test_secrets_are_unique_per_key() -> None:
    tokens = {keys.mint(uuid7()).token for _ in range(50)}

    assert len(tokens) == 50


def test_prefix_carries_no_secret_material() -> None:
    minted = keys.mint(uuid7())
    _, _, secret = minted.token.split("_", 2)

    assert minted.prefix == f"mg_{minted.key_id.hex[:8]}"
    assert secret not in minted.prefix


def test_underscores_in_the_secret_survive_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``token_urlsafe`` emits ``_``; splitting on every underscore would corrupt it."""
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _: "aa_bb_cc")
    minted = keys.mint(uuid7())

    parsed = keys.parse(minted.token)

    assert parsed is not None
    assert parsed.secret == "aa_bb_cc"
    assert keys.verify(parsed.secret, minted.key_hash)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "sk-not-ours",
        "mg_",
        "mg_deadbeef",  # no secret
        "mg_not-a-uuid_secret",
        "mg_" + "0" * 32 + "_",  # empty secret
        "Bearer mg_x_y",
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert keys.parse(token) is None


def test_wrong_secret_does_not_verify() -> None:
    minted = keys.mint(uuid7())

    assert not keys.verify("wrong", minted.key_hash)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer mg_abc", "mg_abc"),
        ("bearer mg_abc", "mg_abc"),
        ("BEARER   mg_abc  ", "mg_abc"),
        ("Basic mg_abc", None),
        ("mg_abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_extraction(header: str | None, expected: str | None) -> None:
    assert keys.bearer_token(header) == expected
