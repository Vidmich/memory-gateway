"""Middleware that exists for security rather than for behaviour.

Three of them, grouped here because they share a property the ones in
:mod:`app.core.middleware` do not: none changes what the application does, and each is
only interesting when something has gone wrong or somebody is trying something.

They are pure ASGI for the same reason the others are — ``BaseHTTPMiddleware`` copies the
response body through a memory stream, which on the streaming path is per-frame overhead
inside a 150 ms budget (SPEC §4.2).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import PayloadTooLarge, error_response

logger = logging.getLogger(__name__)

CONTROL_PLANE_PREFIX = "/api/"

#: FastAPI's own documentation pages load Swagger UI from a CDN and bootstrap it with an
#: inline ``<script>``. Both are refused by the policy below, which is the policy working
#: — so those paths are exempt rather than the policy being weakened for everything else.
#: They do not exist in production, where ``docs_url`` is ``None``.
CSP_EXEMPT_PATHS = ("/docs", "/redoc", "/openapi.json")

#: The SPA's policy.
#:
#: ``script-src 'self'`` with no ``'unsafe-inline'`` is the load-bearing directive and the
#: reason the build must produce no inline script — Vite emits a module tag pointing at a
#: hashed file, so it already does, and a test asserts the built ``index.html`` still
#: contains none.
#:
#: ``style-src`` *does* allow inline, and that is a real weakening rather than an
#: oversight: React writes ``style`` attributes for anything computed — a chart's bar
#: width, a progress bar's fill — and those are inline styles under CSP. The exposure from
#: inline CSS is styling attacks, not code execution, which is a different order of
#: problem from what ``script-src`` is holding.
#:
#: ``connect-src 'self'`` is honest here only because the SPA is served from the API
#: process; a split-origin deployment has to add its API origin.
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """Headers every response carries.

    ``X-Frame-Options`` duplicates the CSP's ``frame-ancestors``, on purpose: the two are
    read by different browsers and the older header is one line.

    HSTS is applied only when an operator has said TLS terminates in front of this process.
    Sending it from a deployment reachable over plain HTTP is how a development host pins
    itself to a scheme it does not serve, and the fix involves clearing browser state on
    every machine that saw it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts_max_age_seconds: int | None = None,
        csp: str | None = DEFAULT_CSP,
    ) -> None:
        self.app = app
        self.headers: list[tuple[str, str]] = [
            # No MIME sniffing: an uploaded file served back with the wrong type must not
            # be executed because a browser thought it looked like a script.
            ("x-content-type-options", "nosniff"),
            # The control plane puts ids in paths; a full referrer would send an
            # organization id to whatever a user clicks through to.
            ("referrer-policy", "strict-origin-when-cross-origin"),
            ("x-frame-options", "DENY"),
        ]
        if hsts_max_age_seconds:
            self.headers.append(
                (
                    "strict-transport-security",
                    f"max-age={hsts_max_age_seconds}; includeSubDomains",
                )
            )
        self.csp = csp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        csp = self.csp if self.csp and not path.startswith(CSP_EXEMPT_PATHS) else None

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    headers.setdefault(name, value)
                if csp:
                    headers.setdefault("content-security-policy", csp)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ControlPlaneCORSMiddleware:
    """CORS, but only in front of ``/api/``.

    The data plane is called server-to-server with a key and is deliberately open to any
    origin — it has no cookie and no browser session to protect. What it must *not* do is
    answer a browser's preflight with ``Access-Control-Allow-Credentials``, because that is
    the one thing that would let a page on another origin spend a logged-in user's session
    against it. An app-wide ``CORSMiddleware`` configured for the control plane's origins
    would do exactly that for ``/g/`` as well, so the wrapper decides by path and passes
    everything else through untouched.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allow_origins: Sequence[str],
        allow_credentials: bool = False,
        allow_methods: Sequence[str] = ("GET",),
        allow_headers: Sequence[str] = (),
        expose_headers: Sequence[str] = (),
    ) -> None:
        # Spelled out rather than forwarded as ``**options``. Starlette's CORS middleware
        # has a dozen knobs and this wrapper only means to be a path filter in front of the
        # four that matter here — a passthrough would make the wrapper's own contract "the
        # same as whatever that class accepts this month".
        self.app = app
        self.cors = CORSMiddleware(
            app,
            allow_origins=list(allow_origins),
            allow_credentials=allow_credentials,
            allow_methods=list(allow_methods),
            allow_headers=list(allow_headers),
            expose_headers=list(expose_headers),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(CONTROL_PLANE_PREFIX):
            await self.app(scope, receive, send)
            return
        await self.cors(scope, receive, send)


class BodySizeLimitMiddleware:
    """Refuse a request body larger than the cap, without reading it.

    Two checks because there are two ways to send a body. A declared ``Content-Length`` is
    refused before the application runs at all — nothing is authenticated, nothing is
    parsed, no memory is allocated for it. A chunked body has no length to check, so the
    bytes are counted as they arrive and the request is failed the moment the count goes
    over; that path raises rather than responding directly, so the failure comes out of the
    application's own error handlers in whichever envelope the route belongs to.

    ``multipart/form-data`` is exempt. That is the upload path, whose per-file ceiling
    (``UPLOAD_MAX_FILE_BYTES``) is enforced while the bytes stream to object storage — a
    cap here would be a second, smaller, differently-worded limit on the same action.
    Ingress carries its own ceiling for that route, which is the layer that keeps a
    hostile upload from reaching this process at all.
    """

    def __init__(self, app: ASGIApp, *, limit_bytes: int) -> None:
        self.app = app
        self.limit = limit_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS", "DELETE"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if headers.get("content-type", "").startswith("multipart/form-data"):
            await self.app(scope, receive, send)
            return

        declared = _content_length(headers)
        if declared is not None and declared > self.limit:
            response = error_response(
                Request(scope),
                status_code=413,
                code="payload_too_large",
                message=_message(self.limit),
            )
            await response(scope, receive, send)
            return

        await self.app(scope, _counted(receive, self.limit), send)


def _counted(receive: Receive, limit: int) -> Receive:
    received = 0

    async def counted() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise PayloadTooLarge(_message(limit))
        return message

    return counted


def _content_length(headers: Headers) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _message(limit: int) -> str:
    return f"Request body is larger than the {limit // 1024} KiB limit."


def html_has_inline_script(markup: str) -> bool:
    """Whether a document carries a ``<script>`` with a body rather than a ``src``.

    Used by the test that guards the CSP: the policy above forbids inline script, so a
    build that started emitting one would be a white page in production and a passing test
    suite anywhere else.
    """
    lowered = markup.lower()
    for start in _positions(lowered, "<script"):
        end = lowered.find(">", start)
        if end == -1:
            continue
        if "src=" in lowered[start:end]:
            continue
        closing = lowered.find("</script>", end)
        body = markup[end + 1 : closing if closing != -1 else None]
        if body.strip():
            return True
    return False


def _positions(text: str, needle: str) -> Iterable[int]:
    index = text.find(needle)
    while index != -1:
        yield index
        index = text.find(needle, index + 1)


__all__ = [
    "CONTROL_PLANE_PREFIX",
    "CSP_EXEMPT_PATHS",
    "DEFAULT_CSP",
    "BodySizeLimitMiddleware",
    "ControlPlaneCORSMiddleware",
    "SecurityHeadersMiddleware",
    "html_has_inline_script",
]
