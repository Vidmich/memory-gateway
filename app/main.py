"""FastAPI application factory.

``create_app`` takes an optional ``Settings`` so tests can build isolated instances
without mutating process state.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.proxy.routes import router as proxy_router
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
from app.services.api_keys import KeyAuthenticator
from app.services.gateways import GatewayResolver
from app.services.proxy import ProxyService

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

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(proxy_router)

    return app


app = create_app()
