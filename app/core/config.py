"""Application configuration.

Settings are validated **at import time**: a missing or malformed variable stops the
process immediately with a readable message rather than surfacing at the first request.
"""

from __future__ import annotations

import base64
import sys
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator
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

    #: Probe presses allowed per user per window, for "Test connection" on a model and
    #: "Test gateway" on an endpoint alike — one setting because they cost the same thing
    #: for the same reason. Every press is an outbound call billed to whoever owns the
    #: model, so it needs a ceiling; a generous one, because getting a base URL right
    #: takes a few tries.
    model_test_max_attempts: int = Field(default=20, ge=1)
    model_test_window_seconds: int = Field(default=60, ge=1)

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
    #: Ceiling on a whole routing chain (SPEC §8.1). Each attempt already carries its
    #: model's own ``timeout_seconds``; this is what stops three targets at 60 s each
    #: from becoming a three-minute request. Larger than the 60 s default model timeout,
    #: so a single-target gateway is unaffected by its existence.
    routing_deadline_seconds: float = Field(default=120.0, gt=0)

    # -- ingestion ---------------------------------------------------------
    #: SPEC §9.2's per-file cap. Enforced while the bytes are streaming, so an oversized
    #: upload is refused rather than stored and then deleted.
    upload_max_file_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    #: Per-organization storage ceiling. ``None`` is unlimited, which is the default for
    #: the same reason task 06's rate limits default to unlimited: a quota nobody set
    #: should not become an outage.
    storage_quota_bytes: int | None = Field(default=None, ge=1024)
    #: Wall-clock cap on reading one file, so a pathological input cannot occupy a worker.
    extraction_timeout_seconds: float = Field(default=120.0, gt=0)
    upload_url_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 3600)

    # -- embeddings (SPEC §9.4) --------------------------------------------
    #: ``openai`` for any OpenAI-compatible ``/embeddings`` endpoint; ``hash`` for the
    #: local lexical embedder, which needs no key and no network. Task 17 moves all of
    #: this into ``platform_settings`` with a reindex flow.
    embedding_provider: Literal["openai", "hash"] = "hash"
    embedding_model: str = "hash-bow"
    #: Must match the model. A mismatch is caught on the first call rather than silently
    #: producing an index that cannot be searched.
    embedding_dimension: int = Field(default=256, ge=8, le=8192)
    embedding_base_url: str = ""
    embedding_api_key: str | None = None
    embedding_batch_size: int = Field(default=96, ge=1, le=2048)
    embedding_max_concurrency: int = Field(default=4, ge=1, le=64)

    # -- worker ------------------------------------------------------------
    job_max_attempts: int = Field(default=5, ge=1, le=20)
    job_backoff_base_seconds: float = Field(default=2.0, gt=0)
    job_backoff_cap_seconds: float = Field(default=300.0, gt=0)
    #: Jobs one worker process runs at once. Ingestion is I/O-bound apart from extraction,
    #: which runs in a thread, so this is about provider concurrency rather than CPU.
    worker_max_jobs: int = Field(default=8, ge=1, le=128)

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

    @model_validator(mode="after")
    def _real_embeddings_in_production(self) -> Settings:
        """The local embedder is lexical, not semantic. It is the right default for a
        development stack with no provider key and completely wrong for a deployment, and
        the failure mode is silent — retrieval simply gets worse. Refusing to start is the
        only version of this warning nobody can miss."""
        if self.environment == "prod" and self.embedding_provider == "hash":
            raise ValueError(
                "EMBEDDING_PROVIDER=hash is a local development embedder and must not be "
                "used in production; configure a real embedding model."
            )
        return self

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
