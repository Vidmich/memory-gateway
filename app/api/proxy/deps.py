"""Dependencies for the data plane.

The collaborators are built once in the lifespan and hung off ``app.state``; these are
just typed lookups. Going through FastAPI dependencies rather than reaching into
``app.state`` inside the route is what lets tests substitute a resolver and an
authenticator — the proxy's behaviour is then testable end to end without a database.
"""

from __future__ import annotations

from fastapi import Request

from app.services.api_keys import KeyAuthenticator
from app.services.gateways import GatewayResolver
from app.services.proxy import ProxyService


def get_resolver(request: Request) -> GatewayResolver:
    resolver: GatewayResolver = request.app.state.gateway_resolver
    return resolver


def get_authenticator(request: Request) -> KeyAuthenticator:
    authenticator: KeyAuthenticator = request.app.state.key_authenticator
    return authenticator


def get_proxy_service(request: Request) -> ProxyService:
    service: ProxyService = request.app.state.proxy_service
    return service
