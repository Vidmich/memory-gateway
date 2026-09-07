"""Control-plane dependencies.

Services are built once in the lifespan and read off ``app.state`` here, which is the
seam tests override. Nothing in this module opens a connection.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.errors import Forbidden, Unauthorized, Validation
from app.core.keys import bearer_token
from app.core.tenancy import Actor, TenantScope
from app.services.auth import AuthenticationRequired, AuthService, Identity, RequestContext
from app.services.catalog import CatalogService
from app.services.directory import DirectoryService
from app.services.gateways import GatewayService
from app.services.monitoring import MonitoringService
from app.services.permissions import Capability, allows

#: Sent on every 401 from the control plane. The SPA keys its refresh-and-retry off the
#: status code, but a bare 401 with no scheme is a protocol violation clients notice.
CHALLENGE = {"www-authenticate": "Bearer"}


#: How a superadmin says "show me this organization" (SPEC §5.2, support access).
#:
#: A header rather than a query or body parameter, and read here rather than in any
#: endpoint, so that no route signature ever takes an organization id it might trust. It
#: is *ignored* for everyone else — not rejected: a 403 would tell an org user the header
#: exists and is worth attacking, while ignoring it simply gives them their own data.
ASSUME_ORGANIZATION_HEADER = "x-assume-organization"


def get_auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth_service
    return service


def get_directory_service(request: Request) -> DirectoryService:
    service: DirectoryService = request.app.state.directory_service
    return service


def get_catalog_service(request: Request) -> CatalogService:
    service: CatalogService = request.app.state.catalog_service
    return service


def get_gateway_service(request: Request) -> GatewayService:
    service: GatewayService = request.app.state.gateway_service
    return service


def get_monitoring_service(request: Request) -> MonitoringService:
    service: MonitoringService = request.app.state.monitoring_service
    return service


def get_settings_from_app(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def request_context(request: Request) -> RequestContext:
    return RequestContext(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


async def require_identity(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> Identity:
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise AuthenticationRequired("Not authenticated.", headers=CHALLENGE)

    try:
        identity = await service.identify(token)
    except Unauthorized as exc:
        # The service deals in sessions, not in HTTP; the challenge header belongs here.
        exc.headers.update(CHALLENGE)
        raise

    # The access log picks this up, so every control-plane line says who did it. Task 15
    # builds the audit trail on the same value.
    scope_state = request.scope.setdefault("state", {})
    scope_state["user_id"] = str(identity.user.id)
    return identity


#: The type every authenticated endpoint annotates its caller with.
CurrentUser = Annotated[Identity, Depends(require_identity)]


def current_actor(request: Request, identity: CurrentUser) -> Actor:
    """Who is acting, and inside which organization.

    The scope comes from the session. The only thing that can widen it is a superadmin
    presenting :data:`ASSUME_ORGANIZATION_HEADER`, and that path goes through
    :meth:`TenantScope.assume`, which logs the access.
    """
    scope = TenantScope.of(identity)

    raw = request.headers.get(ASSUME_ORGANIZATION_HEADER)
    if raw and scope.is_platform:
        try:
            organization_id = uuid.UUID(raw)
        except ValueError as exc:
            raise Validation("Malformed organization id.", param="x-assume-organization") from exc
        scope = scope.assume(organization_id, actor_user_id=identity.user.id)

    return Actor(user_id=identity.user.id, scope=scope)


CurrentActor = Annotated[Actor, Depends(current_actor)]


def require_capability(
    *capabilities: Capability,
) -> Callable[[Identity], Awaitable[Identity]]:
    """Gate a route on the permission matrix rather than on a list of roles.

    Naming a capability keeps the "who may do this" decision in
    :mod:`app.services.permissions`, where every row is asserted by a test, instead of
    spreading role names through the routing layer where a new role would have to be
    added to each one.
    """

    async def dependency(identity: CurrentUser) -> Identity:
        missing = [
            capability for capability in capabilities if not allows(identity.user.role, capability)
        ]
        if missing:
            # 403, not 404: the caller is inside the right organization and the resource
            # is not hidden from them — they simply may not do this. Hiding it would make
            # "your role cannot" indistinguishable from "it does not exist", which is the
            # opposite of what cross-tenant access needs.
            raise Forbidden("Your role does not allow this.")
        return identity

    return dependency
