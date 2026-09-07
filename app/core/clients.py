"""Long-lived connections to the backing services.

They are created once during the lifespan and hung off ``app.state`` so every request
reuses the same pools. Connecting per request is the single easiest way to blow the
latency budget in SPEC §4.2.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
import httpx
from botocore.client import Config as BotoConfig
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine, create_session_factory
from app.services.job_queue import create_job_pool

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = Any


@dataclass
class Clients:
    """Everything the app talks to over a socket."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    qdrant: AsyncQdrantClient
    storage: S3Client
    bucket: str
    http: httpx.AsyncClient
    #: The job queue's own Redis client. Separate from ``redis`` because arq needs raw
    #: bytes and its own serializers, where everything else in the process wants decoded
    #: strings — one client configured for both would be configured for neither.
    jobs: Any

    @classmethod
    def create(cls, settings: Settings) -> Clients:
        engine = create_engine(settings)
        return cls(
            engine=engine,
            session_factory=create_session_factory(engine),
            redis=Redis.from_url(settings.redis_url, decode_responses=True),
            qdrant=AsyncQdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                # The client otherwise makes a blocking version-check call while being
                # constructed, which would put a network round-trip inside app startup
                # and fail the process when Qdrant is merely slow to come up.
                check_compatibility=False,
            ),
            storage=create_storage_client(settings),
            bucket=settings.s3_bucket,
            http=create_http_client(settings),
            jobs=create_job_pool(settings.redis_url),
        )

    async def aclose(self) -> None:
        """Close every pool.

        Failures are isolated: one client that cannot shut down cleanly must not strand
        the others open, which would leak connections across a restart loop.
        """
        for name, close in (
            ("upstream-http", self.http.aclose()),
            ("redis", self.redis.aclose()),
            ("jobs", self.jobs.aclose()),
            ("qdrant", self.qdrant.close()),
            ("postgres", self.engine.dispose()),
        ):
            try:
                await close
            except Exception:
                logger.warning("failed to close client", extra={"client": name}, exc_info=True)
        try:
            self.storage.close()
        except Exception:
            logger.warning("failed to close client", extra={"client": "storage"}, exc_info=True)


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    """The pool every upstream call shares.

    One client for the process, not one per request: a fresh TLS handshake per completion
    would spend most of the 150 ms budget in SPEC §4.2 before the provider sees anything.
    The timeout is per-request instead, carried on each prepared request's extensions,
    because it is a property of the upstream model rather than of the pool.
    """
    return httpx.AsyncClient(
        timeout=None,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive_connections,
            keepalive_expiry=30.0,
        ),
        headers={"user-agent": f"{settings.service_name}/{settings.version}"},
    )


def create_storage_client(settings: Settings) -> S3Client:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},  # MinIO does not do virtual-host style
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )
