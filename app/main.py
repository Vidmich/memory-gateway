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
from app.services.api_keys import KeyAuthenticator
from app.services.auth import AuthService
from app.services.auth_provider import LocalPasswordProvider
from app.services.auth_store import PostgresAuthStore
from app.services.catalog import CatalogService
from app.services.catalog_store import PostgresCatalogStore
from app.services.directory import DirectoryService
from app.services.directory_store import PostgresDirectoryStore
from app.services.gateways import GatewayResolver
from app.services.login_throttle import LoginThrottle, RedisThrottleStore
from app.services.model_probe import ModelProbe
from app.services.proxy import ProxyService
from app.services.rate_limit import FixedWindowLimiter

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

        # Built once, here, so a request pays for a dict lookup rather than for wiring.
        secret_box = SecretBox.from_settings(settings)
        app.state.gateway_resolver = GatewayResolver(clients.session_factory, secret_box)
        app.state.key_authenticator = KeyAuthenticator(clients.session_factory)
        app.state.proxy_service = ProxyService(clients.http)

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
            PostgresDirectoryStore(clients.session_factory),
            hasher=hasher,
            settings=settings,
        )
        app.state.catalog_service = CatalogService(
            PostgresCatalogStore(clients.session_factory),
            secret_box=secret_box,
            # The same pool the proxy uses, so a probe warms the connection a real
            # request will reuse — and so a misconfigured pool fails in both places.
            probe=ModelProbe(clients.http),
            test_limiter=FixedWindowLimiter(
                store=RedisThrottleStore(clients.redis),
                action="model-test",
                limit=settings.model_test_max_attempts,
                window_seconds=settings.model_test_window_seconds,
            ),
            settings=settings,
        )

        logger.info("service started", extra={"environment": settings.environment})
        try:
            yield
        finally:
            # Fire-and-forget writes (`last_used_at`) get a moment to land before the
            # pools they need are closed underneath them.
            await background.drain()
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
            expose_headers=["x-gateway-request-id"],
        )

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(build_control_router())
    app.include_router(proxy_router)
    # Last: the SPA mount is at "/" and matches everything the routers above did not.
    mount_spa(app, settings.web_dist_dir)

    return app


app = create_app()
