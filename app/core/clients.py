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
from app.core.ssrf import GuardedTransport
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
    #: The pool for endpoints *this deployment* configured — the embedding provider today.
    #: Separate from ``http`` because that one refuses to connect to a private address
    #: (:mod:`app.core.ssrf`) and these endpoints are routinely on one: an embedding service
    #: on the cluster network is the normal deployment, not an attack. Splitting the clients
    #: rather than exempting a host inside one guard keeps the distinction at the seam where
    #: it is decided — a tenant chose that URL, an operator chose this one — instead of in a
    #: list somebody has to keep in step. It is also a pool of its own, which is a small
    #: bonus on the retrieval path: an embedding call no longer queues behind completions.
    internal: httpx.AsyncClient
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
            internal=create_internal_client(settings),
            jobs=create_job_pool(settings.redis_url),
        )

    async def aclose(self) -> None:
        """Close every pool.

        Failures are isolated: one client that cannot shut down cleanly must not strand
        the others open, which would leak connections across a restart loop.
        """
        for name, close in (
            ("upstream-http", self.http.aclose()),
            ("internal-http", self.internal.aclose()),
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
    """The pool every upstream call shares, behind the SSRF guard.

    One client for the process, not one per request: a fresh TLS handshake per completion
    would spend most of the 150 ms budget in SPEC §4.2 before the provider sees anything.
    The timeout is per-request instead, carried on each prepared request's extensions,
    because it is a property of the upstream model rather than of the pool.

    ``follow_redirects=False`` predates the guard and is still load-bearing beside it: a
    provider that answers a completion with a 302 is misconfigured, and following one would
    reach an address chosen by the *response* rather than by the operator — a check the
    guard would then have to make all over again on a URL nobody configured.
    """
    return httpx.AsyncClient(
        transport=GuardedTransport(
            httpx.AsyncHTTPTransport(
                limits=_limits(settings),
                # httpx retries only on connection failures, never on a request that
                # reached the server. Two is enough to absorb a pool connection the
                # provider closed while it sat idle, which is the common case.
                retries=2,
            ),
            policy=settings.upstream_url_policy,
        ),
        timeout=None,
        follow_redirects=False,
        limits=_limits(settings),
        headers={"user-agent": f"{settings.service_name}/{settings.version}"},
    )


def create_internal_client(settings: Settings) -> httpx.AsyncClient:
    """The pool for endpoints this deployment configured, with no guard on it.

    The embedding provider today. Its URL comes from the environment, and a deployment that
    runs its own embedding service on the cluster network — which is the normal shape — has
    a perfectly good reason to point at a private address.
    """
    return httpx.AsyncClient(
        timeout=None,
        follow_redirects=False,
        limits=_limits(settings),
        headers={"user-agent": f"{settings.service_name}/{settings.version}"},
    )


def _limits(settings: Settings) -> httpx.Limits:
    return httpx.Limits(
        max_connections=settings.upstream_max_connections,
        max_keepalive_connections=settings.upstream_max_keepalive_connections,
        keepalive_expiry=30.0,
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
