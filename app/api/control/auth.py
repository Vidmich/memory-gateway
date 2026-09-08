"""``/api/v1/auth`` — the only endpoints a browser can reach without an access token.

The split between the two routers below is the safety property: ``public_router`` is
short and explicit, and everything else in the control plane hangs off the authenticated
router in ``app/api/control/router.py``. A new endpoint is therefore protected unless
somebody deliberately puts it here.

Why the refresh token lives in a cookie and the access token does not: a cookie is
readable by any XSS that lands on the page unless it is ``HttpOnly``, and an ``HttpOnly``
cookie cannot carry a token that JavaScript has to attach to requests. So the long-lived
half is a cookie the script cannot read, and the short-lived half is held in memory and
lost on reload. Neither is in ``localStorage``, which is readable by definition.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.control.deps import (
    CHALLENGE,
    CurrentUser,
    get_auth_service,
    get_settings_from_app,
    request_context,
)
from app.core.config import Settings
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    SessionResponse,
    UserSummary,
)
from app.services.auth import (
    SESSION_OVER,
    AuthService,
    IssuedSession,
    RequestContext,
    SessionExpired,
)
from app.services.auth_provider import PasswordCredentials

REFRESH_COOKIE = "mg_refresh"
#: Scoped to the auth endpoints, so the cookie is not attached to — and cannot be
#: exfiltrated through — every other control-plane request.
REFRESH_COOKIE_PATH = "/api/v1/auth"

public_router = APIRouter(prefix="/auth", tags=["auth"])
router = APIRouter(prefix="/auth", tags=["auth"])

_Service = Annotated[AuthService, Depends(get_auth_service)]
_Context = Annotated[RequestContext, Depends(request_context)]
_Settings = Annotated[Settings, Depends(get_settings_from_app)]


@public_router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    response: Response,
    service: _Service,
    context: _Context,
    settings: _Settings,
) -> SessionResponse:
    issued = await service.login(
        PasswordCredentials(email=str(body.email), password=body.password),
        context=context,
        remember=body.remember,
    )
    set_refresh_cookie(response, issued, settings)
    return session_response(issued)


@public_router.post("/refresh", response_model=SessionResponse)
async def refresh(
    request: Request,
    response: Response,
    service: _Service,
    context: _Context,
    settings: _Settings,
) -> SessionResponse:
    """Exchange the refresh cookie for a new access token and a new refresh token.

    Deliberately unauthenticated: by the time a client calls this, its access token has
    expired, which is the only reason it is calling.
    """
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise SessionExpired(SESSION_OVER, headers=_expired_session_headers(settings))

    try:
        issued = await service.refresh(token, context=context)
    except SessionExpired as exc:
        # The cookie has to be cleared on the *error* response, and an exception skips
        # the injected one entirely — the handler in app/core/errors.py builds its own.
        # Leaving a dead token in the browser means the SPA retries a doomed refresh on
        # every reload.
        exc.headers.update(_expired_session_headers(settings))
        raise

    set_refresh_cookie(response, issued, settings)
    return session_response(issued)


@public_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    service: _Service,
    settings: _Settings,
) -> None:
    """Revoke the session family and clear the cookie.

    Unauthenticated and idempotent on purpose: a user whose access token has already
    expired must still be able to end their session, and a logout that returns 401 leaves
    the browser holding a live refresh cookie.

    Returns ``None`` rather than a ``Response``: FastAPI merges the injected response's
    headers only on that path, and returning one directly would drop the Set-Cookie.
    """
    await service.logout(request.cookies.get(REFRESH_COOKIE))
    _clear_refresh_cookie(response, settings)


@router.get("/me", response_model=UserSummary)
async def me(current: CurrentUser) -> UserSummary:
    return UserSummary.of(current.user, current.organization)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChangeRequest,
    current: CurrentUser,
    service: _Service,
    context: _Context,
) -> None:
    """Change your own password and sign every *other* session out.

    Other sessions keep working for up to one access-token TTL: their refresh tokens are
    dead immediately, but an access token already issued is self-contained. Fifteen
    minutes is the documented cost of not doing a token lookup on the data plane; if that
    is ever unacceptable, the fix is a revocation list, not a shorter TTL.
    """
    await service.change_password(
        current.user.id,
        current_password=body.current_password,
        new_password=body.new_password,
        keep_family_id=current.family_id,
        # For the audit event: SPEC §10.4 records the address a change came from, and a
        # password change is the one this matters most for.
        context=context,
    )


def session_response(issued: IssuedSession) -> SessionResponse:
    """Shared with invitation acceptance, which also opens a session (see
    ``app/api/control/directory.py``)."""
    return SessionResponse(
        access_token=issued.access_token,
        expires_at=issued.access_expires_at,
        expires_in=_seconds_until(issued.access_expires_at),
        user=UserSummary.of(issued.user, issued.organization),
    )


def set_refresh_cookie(response: Response, issued: IssuedSession, settings: Settings) -> None:
    """Public because invitation acceptance signs the new member in too, and a second
    copy of these flags is a second thing to get wrong."""
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        # No Max-Age without "remember me": the cookie dies with the browser session.
        max_age=_max_age(issued) if issued.persistent else None,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        # Lax, not Strict: Strict would drop the cookie when the user arrives by
        # following a link into the app, logging them out for no security gain — the
        # endpoint is a POST that no cross-site navigation can trigger.
        samesite="lax",
        secure=settings.is_production,
    )


def _expired_session_headers(settings: Settings) -> dict[str, str]:
    """Headers for a 401 that should also end the browser's session."""
    probe = Response()
    _clear_refresh_cookie(probe, settings)
    return {**CHALLENGE, "set-cookie": probe.headers["set-cookie"]}


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
    )


def _max_age(issued: IssuedSession) -> int:
    return _seconds_until(issued.refresh_expires_at)


def _seconds_until(moment: datetime) -> int:
    return max(0, int((moment - datetime.now(UTC)).total_seconds()))
