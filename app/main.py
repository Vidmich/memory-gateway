"""FastAPI application factory.

``create_app`` takes an optional ``Settings`` so tests can build isolated instances
without mutating process state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.control.router import build_control_router
from app.api.health import router as health_router
from app.api.proxy.routes import router as proxy_router
from app.api.spa import mount_spa
from app.core import background
from app.core.clients import Clients
from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import build_metrics
from app.core.middleware import (
    AccessLogMiddleware,
    MetricsMiddleware,
    RequestIdMiddleware,
)
from app.core.passwords import build_hasher
from app.services.api_keys import KeyAuthenticator, LastUsedRecorder
from app.services.audit import count_audit_failures_with
from app.services.audit_service import AuditService
from app.services.audit_store import PostgresAuditStore
from app.services.auth import AuthService
from app.services.auth_provider import LocalPasswordProvider
from app.services.auth_store import PostgresAuthStore
from app.services.catalog import CatalogService
from app.services.catalog_store import PostgresCatalogStore
from app.services.connectors import ConnectorService
from app.services.directory import DirectoryService
from app.services.directory_store import PostgresDirectoryStore
from app.services.distillation_service import DistillationService
from app.services.end_user_resolver import EndUserResolver, RequestCounters
from app.services.end_user_store import PostgresEndUserStore
from app.services.end_users import EndUserService
from app.services.fact_vectors import QdrantFactVectorStore
from app.services.facts import FactRecaller
from app.services.gateway_probe import ProxyGatewayProbe
from app.services.gateway_resolver import (
    CachedGatewayResolver,
    DatabaseGatewayResolver,
    GatewayCache,
)
from app.services.gateway_store import PostgresGatewayStore
from app.services.gateways import GatewayService
from app.services.impersonation import SupportAccessRecorder
from app.services.limit_store import RedisLimitStore
from app.services.limiter import RateLimiter
from app.services.limits_service import LimitsService
from app.services.log_store import PostgresLogWriter
from app.services.login_throttle import LoginThrottle, RedisThrottleStore
from app.services.memory_preview import MemoryPreview
from app.services.metrics_store import PostgresMetricsRepository
from app.services.model_probe import ModelProbe
from app.services.monitoring import MonitoringService, RedisSummaryCache
from app.services.platform_settings import ceilings_of
from app.services.proxy import ProxyService
from app.services.rate_limit import FixedWindowLimiter
from app.services.request_log import LogFlusher, LogQueue, RequestLogService
from app.services.retrieval import MemoryService, Retriever
from app.services.routing import Router
from app.services.tokenizer import build_tokenizer
from app.workers.runtime import (
    build_distillation,
    build_ingestion,
    build_platform,
    build_platform_settings,
    build_queue,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        version=settings.version,
    )
    metrics = build_metrics(service_name=settings.service_name, version=settings.version)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        clients = Clients.create(settings)
        app.state.clients = clients

        # First, because the embedder every other object is built with depends on it: the
        # platform's embedding choice is a row, and a process that built its pipeline from
        # the environment and then discovered the row would be embedding with one model
        # and searching with another until it restarted. `warm` never raises — a database
        # that is not up yet leaves the process on its environment bootstrap, which is
        # exactly what it ran on before this table existed.
        platform_settings = build_platform_settings(clients, settings)
        await platform_settings.warm()
        platform_settings.start()
        app.state.platform_settings = platform_settings

        # Built once, here, so a request pays for a dict lookup rather than for wiring.
        secret_box = SecretBox.from_settings(settings)
        gateway_cache = GatewayCache(clients.redis)
        source = DatabaseGatewayResolver(clients.session_factory, secret_box)
        # The data plane reads through the cache; the control plane holds the same cache
        # object so a write bumps the version the very next request checks.
        resolver = CachedGatewayResolver(source, gateway_cache)
        app.state.gateway_resolver = resolver
        app.state.key_authenticator = KeyAuthenticator(
            clients.session_factory, LastUsedRecorder(clients.redis, settings=settings)
        )
        # The tokenizer the assembler measures budgets with. Built once because
        # `tiktoken` loads a vocabulary on first use and a per-request build would repeat
        # that lookup on the hot path.
        tokenizer = build_tokenizer()
        proxy_service = ProxyService(clients.http, tokenizer=tokenizer)
        app.state.proxy_service = proxy_service
        upstream_router = Router(
            proxy_service,
            deadline_seconds=settings.routing_deadline_seconds,
            metrics=metrics.routing,
        )
        app.state.upstream_router = upstream_router
        # SPEC §11. One store for every gateway, and the ceilings that protect the
        # operator's own credential read from configuration rather than from a database:
        # they are the platform's policy, not a tenant's setting, and a tenant must not be
        # able to raise them.
        limit_store = RedisLimitStore(clients.redis)
        rate_limiter = RateLimiter(
            limit_store,
            # A function rather than a value: since task 17 the ceilings live in
            # `platform_settings`, and an operator lowering one has to bind on the next
            # request rather than on the next deploy. The limiter reads the cached
            # snapshot, so this stays a dictionary lookup on the request path.
            ceilings=lambda: ceilings_of(platform_settings.snapshot),
            fail_open=settings.rate_limit_fail_open,
            metrics=metrics.rate_limits,
        )
        app.state.rate_limiter = rate_limiter

        # Ingestion. Built here, in the API process, because two of its operations are
        # synchronous — deleting a document and reconciling a connector — and the worker
        # builds the same objects from the same function, so the two cannot drift.
        ingestion = build_ingestion(
            clients,
            settings,
            queue=build_queue(clients.jobs),
            metrics=metrics.extraction,
            embedding=platform_settings.snapshot.embedding,
        )
        app.state.ingestion = ingestion
        # Conversation memory's write half, from the same builder the worker uses. Built
        # *before* the log flusher because the flusher holds its trigger: a transcript that
        # has just been committed is the event that arms a distillation pass.
        distillation = build_distillation(
            clients, settings, ingestion=ingestion, metrics=metrics.distillation
        )
        app.state.distillation = distillation

        # The operator's half. Built after ingestion and distillation because it reuses
        # their vector stores and their object store: a sweep that read a different index
        # from the one ingestion writes would report every point as an orphan.
        platform = build_platform(
            clients,
            settings,
            ingestion=ingestion,
            distillation=distillation,
            platform_settings=platform_settings,
            queue=ingestion.queue,
            metrics=metrics.maintenance,
        )
        app.state.platform = platform
        app.state.platform_service = platform.service

        # The request log's write half. The queue is created before the flusher because
        # the flusher only reads from it, and started here rather than lazily so a
        # process that has accepted a request has already proved it can drain one.
        log_queue = LogQueue(metrics=metrics.logs)
        request_logs = RequestLogService(
            log_queue,
            LogFlusher(
                log_queue,
                PostgresLogWriter(clients.session_factory),
                metrics=metrics.logs,
                # Task 13's one touch point with the serving half, and it is on the far
                # side of the commit: a pass is armed only once the transcript it will read
                # exists. Failures here are swallowed — see ``LogFlusher._notify``.
                subscriber=distillation.trigger,
            ),
        )
        request_logs.start()
        app.state.request_logs = request_logs
        metrics_repository = PostgresMetricsRepository(clients.session_factory)
        app.state.monitoring_service = MonitoringService(
            metrics_repository,
            cache=RedisSummaryCache(clients.redis),
        )
        # Retrieval reads the same index ingestion writes, through the same two ports —
        # which is what makes "did my upload become searchable" and "does the gateway see
        # it" the same question rather than two systems that agree by convention.
        retriever = Retriever(ingestion.embedder, ingestion.vectors, metrics=metrics.retrieval)

        # Conversation memory (SPEC §6.1 B). Its own store, its own collection, and its
        # own resolver in front — identity is not memory, so a gateway that has memory
        # switched off still attributes its traffic on the end-users screen.
        end_user_store = PostgresEndUserStore(clients.session_factory)
        fact_vectors = QdrantFactVectorStore(clients.qdrant)
        counters = RequestCounters(end_user_store)
        counters.start()
        app.state.end_user_counters = counters
        app.state.end_user_resolver = EndUserResolver(end_user_store, counters=counters)
        app.state.memory_service = MemoryService(
            retriever,
            recaller=FactRecaller(
                ingestion.embedder, fact_vectors, end_user_store, metrics=metrics.retrieval
            ),
            metrics=metrics.retrieval,
        )
        app.state.end_user_service = EndUserService(
            end_user_store,
            vectors=fact_vectors,
            # The same embedder recall uses. A memory browser searching with a different
            # model would be a screen that agrees with itself and disagrees with what a
            # request actually retrieves.
            embedder=ingestion.embedder,
            logs=metrics_repository,
        )
        app.state.connector_service = ConnectorService(
            ingestion.store,
            objects=ingestion.objects,
            vectors=ingestion.vectors,
            embedder=ingestion.embedder,
            pipeline=ingestion.pipeline,
            queue=ingestion.queue,
            settings=ingestion.settings,
        )

        directory_store = PostgresDirectoryStore(clients.session_factory)
        app.state.distillation_service = DistillationService(
            distillation.store,
            # The organization row is where the settings live, so this reads and writes
            # through the same store the Organizations screen does.
            directory=directory_store,
            end_users=end_user_store,
            distiller=distillation.distiller,
            models=distillation.models,
            debouncer=distillation.debouncer,
            # So that changing the debounce delay applies to the next request rather than
            # to the one after the flusher's cache expires.
            cache=distillation.trigger,
        )

        # SPEC §10.4. The read half only: an event is written into whichever transaction
        # is making the change, through the recorder mixins the stores carry, so there is
        # no writer to wire here. The one exception is a superadmin opening a customer's
        # organization, which happens on a read and has no transaction to join.
        audit_store = PostgresAuditStore(clients.session_factory)
        app.state.audit_service = AuditService(
            audit_store,
            export_limiter=FixedWindowLimiter(
                store=RedisThrottleStore(clients.redis),
                action="audit-export",
                limit=settings.audit_export_max_attempts,
                window_seconds=settings.audit_export_window_seconds,
            ),
        )
        app.state.support_access = SupportAccessRecorder(
            audit_store,
            # Redis, so one support session across two replicas is still one event.
            RedisThrottleStore(clients.redis),
        )
        # An event that could not be built is swallowed rather than failing somebody's
        # save; this is what keeps that from being invisible. Process-level because the
        # code that increments it is a mixin on a transaction — see the function's own
        # docstring.
        count_audit_failures_with(metrics.audit.failures)

        hasher = build_hasher(settings)
        app.state.auth_service = AuthService(
            PostgresAuthStore(clients.session_factory),
            provider=LocalPasswordProvider(hasher),
            hasher=hasher,
            # Redis, not memory: the counters have to be shared, or N replicas mean N
            # times the allowed attempts.
            throttle=LoginThrottle(RedisThrottleStore(clients.redis), settings),
            settings=settings,
        )
        app.state.directory_service = DirectoryService(
            directory_store,
            hasher=hasher,
            settings=settings,
        )
        app.state.catalog_service = CatalogService(
            PostgresCatalogStore(clients.session_factory),
            secret_box=secret_box,
            # The same pool the proxy uses, so a probe warms the connection a real
            # request will reuse — and so a misconfigured pool fails in both places.
            probe=ModelProbe(clients.http),
            # A model's base URL or credential changing has to reach every gateway that
            # points at it, including ones in other organizations for a global model.
            cache=gateway_cache,
            test_limiter=FixedWindowLimiter(
                store=RedisThrottleStore(clients.redis),
                action="model-test",
                limit=settings.model_test_max_attempts,
                window_seconds=settings.model_test_window_seconds,
            ),
            settings=settings,
        )
        gateway_store = PostgresGatewayStore(clients.session_factory)
        # The Limits screen and the dashboard's near-limit card. The configuration comes
        # from the database rather than the cached payload, and the usage comes straight
        # out of the buckets the limiter consumes from — see the module docstring for why
        # those two sources are deliberately different.
        app.state.limits_service = LimitsService(
            gateway_store,
            buckets=limit_store,
            # The same source the limiter reads, so the number on the Limits screen is the
            # number the request path just enforced rather than one from process start.
            ceilings=lambda: ceilings_of(platform_settings.snapshot),
        )
        # The editor's Memory section: the same retriever and the same assembler the data
        # plane uses, so what it shows is what a request would inject.
        app.state.memory_preview = MemoryPreview(
            gateway_store, memory=app.state.memory_service, tokenizer=tokenizer
        )
        app.state.gateway_service = GatewayService(
            gateway_store,
            # Through the *cached* resolver on purpose: "Test gateway" has to exercise
            # what a customer's request exercises, cache included.
            probe=ProxyGatewayProbe(resolver, upstream_router),
            cache=gateway_cache,
            test_limiter=FixedWindowLimiter(
                store=RedisThrottleStore(clients.redis),
                action="gateway-test",
                limit=settings.model_test_max_attempts,
                window_seconds=settings.model_test_window_seconds,
            ),
            settings=settings,
            # SPEC §10.2's platform ceiling, read from the cached snapshot for the same
            # reason the rate-limit one is: an operator lowering it has to bind on the
            # next save rather than on the next deploy.
            retention_ceilings=lambda: platform_settings.snapshot.retention,
        )

        logger.info("service started", extra={"environment": settings.environment})
        try:
            yield
        finally:
            # Order matters: the flusher writes through the same pool `clients.aclose()`
            # closes, so the last half-second of traffic has to reach the database before
            # the connections it needs are taken away.
            await request_logs.stop()
            # Before the pools it reads through are closed underneath its next tick.
            await platform_settings.stop()
            # Same reasoning one table along: the last window of end-user sightings has to
            # reach the database before the connections it needs are taken away.
            await counters.stop()
            # Fire-and-forget writes (`last_used_at`) get a moment to land before the
            # pools they need are closed underneath them.
            await background.drain()
            # Before the clients, because a child process holds nothing of theirs but is
            # a process: leaving it behind on a rolling restart leaks one per replica.
            await ingestion.aclose()
            await clients.aclose()
            logger.info("service stopped")

    app = FastAPI(
        title="Memory Gateway",
        version=settings.version,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.metrics = metrics

    # Middleware runs bottom-up: the request id is bound first so every other layer,
    # including the exception handlers, can log it.
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    if settings.cors_origins:
        # Only for a split-origin deployment (the Vite dev server on another port).
        # In production the SPA is served from this process, so the list is empty and
        # the middleware is never added.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,  # the refresh cookie
            allow_methods=["*"],
            allow_headers=["authorization", "content-type"],
            expose_headers=[
                "x-gateway-request-id",
                # SPEC §11's budget headers. Without these a browser client can read the
                # 429 and not the numbers that would have let it avoid one.
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
                "retry-after",
            ],
        )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(build_control_router())
    app.include_router(proxy_router)
    # Last: the SPA mount is at "/" and matches everything the routers above did not.
    mount_spa(app, settings.web_dist_dir)

    return app


app = create_app()
