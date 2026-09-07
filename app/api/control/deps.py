"""Control-plane dependencies.

Services are built once in the lifespan and read off ``app.state`` here, which is the
seam tests override. Nothing in this module opens a connection.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.errors import Unauthorized
from app.core.keys import bearer_token
from app.services.auth import AuthenticationRequired, AuthService, Identity, RequestContext

#: Sent on every 401 from the control plane. The SPA keys its refresh-and-retry off the
#: status code, but a bare 401 with no scheme is a protocol violation clients notice.
CHALLENGE = {"www-authenticate": "Bearer"}


def get_auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth_service
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
