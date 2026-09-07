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

    # -- control-plane auth ------------------------------------------------
    # Argon2id cost. Defaults follow the OWASP guidance for a server that also does
    # other work; raise `password_memory_cost_kib` first — memory hardness is what
    # actually costs an attacker with GPUs. Raising either is safe: existing hashes
    # carry their own parameters and are upgraded on the owner's next login.
    password_time_cost: int = Field(default=3, ge=1)
    password_memory_cost_kib: int = Field(default=65536, ge=8)
    password_parallelism: int = Field(default=1, ge=1)

    access_token_ttl_seconds: int = Field(default=15 * 60, ge=60)
    refresh_token_ttl_seconds: int = Field(default=14 * 24 * 3600, ge=300)
    #: "Remember me" unchecked: the refresh cookie becomes a session cookie and the
    #: token itself expires sooner.
    refresh_token_short_ttl_seconds: int = Field(default=12 * 3600, ge=300)

    #: Failed logins allowed per window before backoff, counted per IP and per email.
    login_max_attempts: int = Field(default=5, ge=1)
    login_attempt_window_seconds: int = Field(default=15 * 60, ge=1)
    login_lockout_seconds: int = Field(default=15 * 60, ge=1)

    # -- addressing --------------------------------------------------------
    public_base_url: str
    #: Origins allowed to call the control plane with credentials. The Vite dev server
    #: runs on a different port, so in dev this is not the same origin as the API.
    cors_origins: tuple[str, ...] = ()
    #: Directory of built SPA assets to serve, if any. Empty in dev, where Vite serves them.
    web_dist_dir: str = ""
    #: Where the UI lives, for links that a person clicks (an invitation, later a password
    #: reset). Falls back to ``public_base_url``, which is correct in production because
    #: the API serves the SPA; in dev the Vite server is on another port, so compose sets
    #: this explicitly. Getting it wrong sends invitees to the API instead of the app.
    app_base_url: str = ""

    # -- upstream calls ----------------------------------------------------
    upstream_max_connections: int = Field(default=200, ge=1)
    upstream_max_keepalive_connections: int = Field(default=50, ge=0)

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
    def ui_base_url(self) -> str:
        return self.app_base_url.rstrip("/") or self.public_base_url

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
