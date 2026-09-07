"""Application configuration.

Settings are validated **at import time**: a missing or malformed variable stops the
process immediately with a readable message rather than surfacing at the first request.
"""

from __future__ import annotations

import base64
import sys
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "prod"]


class Settings(BaseSettings):
    """Twelve-factor configuration, one field per environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # -- service -----------------------------------------------------------
    environment: Environment = "dev"
    log_level: str = "INFO"
    service_name: str = "memory-gateway"
    version: str = "0.1.0"

    # -- postgres ----------------------------------------------------------
    database_url: str
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_echo: bool = False

    # -- redis -------------------------------------------------------------
    redis_url: str

    # -- qdrant ------------------------------------------------------------
    qdrant_url: str
    qdrant_api_key: str | None = None

    # -- object storage ----------------------------------------------------
    s3_endpoint: str
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = "us-east-1"

    # -- secrets -----------------------------------------------------------
    encryption_master_key: str
    jwt_signing_key: str = Field(min_length=32)

    # -- addressing --------------------------------------------------------
    public_base_url: str

    # -- probes ------------------------------------------------------------
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("must be a postgresql+asyncpg:// URL (the app uses async SQLAlchemy)")
        return value

    @field_validator("encryption_master_key")
    @classmethod
    def _require_32_byte_key(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception as exc:  # surfaced to the user as a validation message
            raise ValueError("must be base64-encoded") from exc
        if len(raw) != 32:
            raise ValueError(f"must decode to exactly 32 bytes, got {len(raw)}")
        return value

    @field_validator("public_base_url", "s3_endpoint", "qdrant_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// URL")
        return value.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"

    @property
    def sync_database_url(self) -> str:
        """Same database, psycopg-free sync form — used by Alembic tooling."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration; the service cannot start:"]
    for error in exc.errors():
        name = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {name.upper()}: {error['msg']}")
    lines.append("See .env.example for the full list of variables.")
    return "\n".join(lines)


def _load_settings() -> Settings:
    # mypy synthesises a keyword-only __init__ for pydantic models (PEP 681), so a bare
    # `Settings()` reads as "missing arguments" even though every value is read from the
    # environment. Unpacking an explicit empty mapping states that intent without an ignore.
    from_environment: dict[str, Any] = {}
    try:
        return Settings(**from_environment)
    except ValidationError as exc:
        print(_format_validation_error(exc), file=sys.stderr)  # logging is not configured yet
        raise SystemExit(1) from exc


settings: Settings = _load_settings()


def get_settings() -> Settings:
    """Dependency-injection friendly accessor for the validated singleton."""
    return settings
