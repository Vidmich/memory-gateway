"""Dependencies for the data plane.

The collaborators are built once in the lifespan and hung off ``app.state``; these are
just typed lookups. Going through FastAPI dependencies rather than reaching into
``app.state`` inside the route is what lets tests substitute a resolver and an
authenticator — the proxy's behaviour is then testable end to end without a database.
"""

from __future__ import annotations

from fastapi import Request

from app.services.api_keys import KeyAuthenticator
from app.services.end_user_resolver import EndUserResolver
from app.services.gateway_resolver import GatewayResolver
from app.services.limiter import RateLimiter
from app.services.proxy import ProxyService
from app.services.request_log import RequestLogService
from app.services.retrieval import MemoryService
from app.services.routing import Router


def get_resolver(request: Request) -> GatewayResolver:
    resolver: GatewayResolver = request.app.state.gateway_resolver
    return resolver


def get_authenticator(request: Request) -> KeyAuthenticator:
    authenticator: KeyAuthenticator = request.app.state.key_authenticator
    return authenticator


def get_proxy_service(request: Request) -> ProxyService:
    service: ProxyService = request.app.state.proxy_service
    return service


def get_router(request: Request) -> Router:
    """The routing executor: the plan walker in front of the proxy.

    Separate from :func:`get_proxy_service` because the two are different jobs and the
    probe uses both — the proxy alone knows how to talk to one provider, and this knows
    which providers to try.
    """
    routing: Router = request.app.state.upstream_router
    return routing


def get_request_logs(request: Request) -> RequestLogService:
    """The request log's write half.

    A dependency rather than a module-level singleton for the same reason as the rest:
    a test substitutes one whose sink is a list, and then asserts on what the proxy
    recorded without a database, a queue or a background task in sight.
    """
    service: RequestLogService = request.app.state.request_logs
    return service


def get_end_users(request: Request) -> EndUserResolver:
    """Who is asking, turned into a row (SPEC §6.2).

    Its own dependency rather than a field on the memory service, because identity is not
    memory: a request through a gateway with conversation memory switched off is still
    attributed on its log row and still counted on the end-users screen, and folding the
    two together would make turning memory off quietly stop the reporting as well.
    """
    resolver: EndUserResolver = request.app.state.end_user_resolver
    return resolver


def get_limiter(request: Request) -> RateLimiter:
    """SPEC §11's enforcement, over one shared Redis.

    A dependency like the rest, and here it earns its keep twice over: a test proves
    "exactly N admitted under concurrent load" against an in-process store with no Redis
    in sight, and proves "with Redis down, requests still succeed" by substituting a
    store whose every method raises.
    """
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def get_memory(request: Request) -> MemoryService:
    """The memory subsystem: document retrieval now, conversation memory from task 12.

    A dependency like the rest, and for the sharpest version of the same reason: a test
    that wants to prove `fail_closed` returns 503 substitutes one whose retriever always
    times out, and does it without Qdrant, an embedding provider, or a clock.
    """
    service: MemoryService = request.app.state.memory_service
    return service
