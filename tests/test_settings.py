"""Configuration must fail at startup, loudly, naming the variable."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, _format_validation_error, _load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every configuration variable so these tests control the whole input.

    Without this, a variable the test means to omit is still supplied by the ambient
    environment and the test passes for the wrong reason.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _env(**overrides: str | None) -> dict[str, str]:
    base = {
        "environment": "test",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
        "qdrant_url": "http://localhost:6333",
        "s3_endpoint": "http://localhost:9000",
        "s3_bucket": "bucket",
        "s3_access_key_id": "key",
        "s3_secret_access_key": "secret",
        "encryption_master_key": "dGVzdC1tYXN0ZXIta2V5LTMyLWJ5dGVzLWxvbmchISE=",
        "jwt_signing_key": "x" * 32,
        "public_base_url": "http://localhost:8000",
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


def _build(**overrides: str | None) -> Settings:
    # `_env_file=None` ignores any local .env so the test controls every value.
    values: dict[str, Any] = {"_env_file": None, **_env(**overrides)}
    return Settings(**values)


def test_valid_configuration_builds() -> None:
    settings = _build()
    assert settings.environment == "test"
    assert settings.sync_database_url.startswith("postgresql://")


def test_missing_required_variable_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        _build(database_url=None)

    assert "database_url" in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql://u:p@localhost/db"),  # sync driver
        ("encryption_master_key", "not-base64!!"),
        ("encryption_master_key", "c2hvcnQ="),  # decodes to 5 bytes
        ("jwt_signing_key", "too-short"),
        ("public_base_url", "localhost:8000"),  # no scheme
        ("qdrant_url", "tcp://localhost:6333"),
        ("environment", "production"),  # not one of the allowed literals
        ("db_pool_size", "0"),
    ],
)
def test_malformed_values_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _build(**{field: value})

    assert field in str(caught.value)


def test_trailing_slash_is_normalized() -> None:
    assert _build(public_base_url="http://localhost:8000/").public_base_url == (
        "http://localhost:8000"
    )


def test_the_ui_base_url_falls_back_to_the_public_one() -> None:
    """Correct in production, where the API serves the SPA from the same origin."""
    settings = _build(public_base_url="http://gateway.example.com")

    assert settings.ui_base_url == "http://gateway.example.com"


def test_the_ui_base_url_can_be_split_from_the_api() -> None:
    """In development the Vite server is on another port, so an invitation link built
    from PUBLIC_BASE_URL would send the invitee to a JSON 404."""
    settings = _build(
        public_base_url="http://localhost:8000", app_base_url="http://localhost:5173/"
    )

    assert settings.ui_base_url == "http://localhost:5173"


def test_error_message_names_the_variable_in_env_form() -> None:
    with pytest.raises(ValidationError) as caught:
        _build(database_url=None)

    message = _format_validation_error(caught.value)
    assert "DATABASE_URL" in message
    assert ".env.example" in message


def test_startup_exits_non_zero_when_configuration_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def settings_ignoring_dotenv(**_: Any) -> Settings:
        values: dict[str, Any] = {"_env_file": None}
        return Settings(**values)

    monkeypatch.setattr("app.core.config.Settings", settings_ignoring_dotenv)

    with pytest.raises(SystemExit) as caught:
        _load_settings()

    assert caught.value.code == 1
