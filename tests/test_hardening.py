"""Security headers, CSP, path-scoped CORS, and the body-size ceiling (task 18).

Every case here is about a response somebody never looks at until it matters. The tests
are therefore written against the *app as assembled* rather than against the middleware
classes in isolation wherever that is possible — the ordering of the stack is half of what
makes these correct, and a unit test of a middleware object cannot see it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.hardening import DEFAULT_CSP, BodySizeLimitMiddleware, html_has_inline_script
from app.main import create_app


def directives(header: str) -> dict[str, str]:
    parsed = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition(" ")
        parsed[name] = value
    return parsed


# ---------------------------------------------------------------------------
# security headers
# ---------------------------------------------------------------------------


async def test_every_response_carries_the_baseline_headers(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


async def test_an_error_response_carries_them_too(client: AsyncClient) -> None:
    """The headers middleware is outermost precisely so this holds: a 404 produced by
    Starlette's router, before any application code runs, is still a response a browser
    renders."""
    response = await client.get("/api/v1/nothing-here")

    assert response.status_code in (401, 404)
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_hsts_is_not_sent_outside_production(client: AsyncClient) -> None:
    """A development host that pins itself to HTTPS is one nobody can reach until they
    clear site data — on every machine that saw the header."""
    response = await client.get("/healthz")

    assert "strict-transport-security" not in response.headers


async def test_hsts_is_sent_in_production() -> None:
    settings = get_settings().model_copy(
        update={"environment": "prod", "embedding_provider": "openai"}
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get("/healthz")

    header = response.headers["strict-transport-security"]
    assert "max-age=" in header
    assert "includeSubDomains" in header


async def test_the_csp_forbids_inline_script(client: AsyncClient) -> None:
    """The load-bearing directive. Everything else in the policy is defence in depth; this
    is the one that makes an injected ``<script>`` inert."""
    policy = directives(DEFAULT_CSP)

    assert policy["script-src"] == "'self'"
    assert "unsafe-inline" not in policy["script-src"]
    assert "unsafe-eval" not in policy["script-src"]
    assert policy["object-src"] == "'none'"
    assert policy["frame-ancestors"] == "'none'"

    response = await client.get("/healthz")
    assert response.headers["content-security-policy"] == DEFAULT_CSP


def test_the_csp_allows_inline_style_and_says_why() -> None:
    """A real weakening rather than an oversight: React writes a ``style`` attribute for
    anything computed — a chart bar's width, a utilisation bar's fill — and inline CSS is
    a styling problem rather than a code-execution one."""
    assert "'unsafe-inline'" in directives(DEFAULT_CSP)["style-src"]


async def test_the_docs_page_is_exempt_from_the_csp(client: AsyncClient) -> None:
    """Swagger UI loads from a CDN and bootstraps itself with an inline script. Both are
    refused by the policy, which is the policy working — so the path is exempt rather than
    the policy weakened for every other response. It does not exist in production."""
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


def test_the_built_spa_contains_no_inline_script() -> None:
    """The CSP is only deployable while this holds. Vite emits a module tag pointing at a
    hashed file, so it does — but a plugin, an analytics snippet or a hand-edited
    ``index.html`` would change that, and the failure mode is a white page in production
    and a green test suite everywhere else."""
    index = Path("web/index.html")
    assert index.is_file()
    assert not html_has_inline_script(index.read_text(encoding="utf-8"))

    built = Path("web/dist/index.html")
    if built.is_file():  # only after `npm run build`
        assert not html_has_inline_script(built.read_text(encoding="utf-8"))


def test_the_inline_script_detector_can_tell_the_difference() -> None:
    assert html_has_inline_script("<script>alert(1)</script>")
    assert not html_has_inline_script('<script type="module" src="/a.js"></script>')
    assert not html_has_inline_script('<script src="/a.js"></script>\n<p>hi</p>')


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.fixture
def cors_settings() -> Settings:
    return get_settings().model_copy(update={"cors_origins": ("https://app.example.com",)})


async def app_with(settings: Settings) -> tuple[FastAPI, AsyncClient]:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    return app, AsyncClient(transport=transport, base_url="http://testserver")


async def test_the_control_plane_answers_a_configured_origin(cors_settings: Settings) -> None:
    app, http = await app_with(cors_settings)
    async with app.router.lifespan_context(app), http:
        response = await http.options(
            "/api/v1/auth/me",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_the_data_plane_never_answers_a_browser_preflight_with_credentials(
    cors_settings: Settings,
) -> None:
    """The reason CORS is scoped by path rather than applied to the whole app.

    The data plane is deliberately open — it is called server-to-server with a key and has
    no cookie to protect. What it must not do is tell a browser that a page on another
    origin may spend a logged-in session against it, which is exactly what an app-wide
    middleware configured for the control plane's origins would say.
    """
    app, http = await app_with(cors_settings)
    async with app.router.lifespan_context(app), http:
        response = await http.options(
            "/g/demo/v1/chat/completions",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert "access-control-allow-credentials" not in response.headers
    assert "access-control-allow-origin" not in response.headers


async def test_an_unconfigured_origin_gets_nothing(cors_settings: Settings) -> None:
    app, http = await app_with(cors_settings)
    async with app.router.lifespan_context(app), http:
        response = await http.options(
            "/api/v1/auth/me",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )

    assert "access-control-allow-origin" not in response.headers


async def test_no_cors_middleware_at_all_when_no_origins_are_configured(
    client: AsyncClient,
) -> None:
    """The production shape: the SPA is served by this process, so it is same-origin and
    there is nothing to allow."""
    response = await client.options(
        "/api/v1/auth/me",
        headers={"Origin": "https://app.example.com", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# request size
# ---------------------------------------------------------------------------
#
# Against a purpose-built app rather than a real route. The middleware has to be exercised
# with a body that is refused *before* anything downstream runs, and the only way to assert
# that is to have something downstream that records whether it did.

LIMIT = 64 * 1024


def limited_app() -> tuple[FastAPI, list[int]]:
    """An app with the real middleware and the real error handlers, and a route that says
    whether it was reached."""
    reached: list[int] = []
    app = FastAPI()

    @app.post("/api/v1/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        reached.append(len(body))
        return {"size": len(body)}

    @app.post("/g/demo/v1/echo")
    async def data_plane_echo(request: Request) -> dict[str, int]:
        body = await request.body()
        reached.append(len(body))
        return {"size": len(body)}

    @app.post("/api/v1/upload")
    async def upload(request: Request) -> dict[str, int]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        reached.append(total)
        return {"size": total}

    register_exception_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, limit_bytes=LIMIT)
    return app, reached


def limited_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_an_oversized_declared_body_is_refused_before_the_route_runs() -> None:
    app, reached = limited_app()
    async with limited_client(app) as http:
        response = await http.post(
            "/api/v1/echo",
            content=b"x" * (LIMIT * 2),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    # Nothing downstream ran: no parsing, no authentication, no allocation for the body.
    assert reached == []


async def test_a_chunked_body_is_refused_as_it_arrives() -> None:
    """A chunked request has no ``Content-Length`` to check, so the only way to enforce a
    ceiling is to count as the bytes come in. The failure comes out of the application's
    own error handlers, which is why it is raised rather than written by the middleware."""

    async def oversized() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * (LIMIT // 2)

    app, reached = limited_app()
    async with limited_client(app) as http:
        response = await http.post(
            "/api/v1/echo",
            content=oversized(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert reached == []


async def test_a_body_inside_the_ceiling_is_untouched() -> None:
    app, reached = limited_app()
    async with limited_client(app) as http:
        response = await http.post(
            "/api/v1/echo",
            content=b"x" * 1024,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert reached == [1024]


async def test_an_upload_is_exempt_because_it_has_its_own_ceiling() -> None:
    """``multipart/form-data`` is the upload path, whose per-file cap is enforced while the
    bytes stream to object storage. A second, smaller, differently-worded limit here would
    refuse a 10 MB PDF the product documents as supported."""
    app, reached = limited_app()
    async with limited_client(app) as http:
        response = await http.post(
            "/api/v1/upload",
            content=b"y" * (LIMIT * 3),
            headers={"content-type": "multipart/form-data; boundary=x"},
        )

    assert response.status_code == 200
    assert reached == [LIMIT * 3]


async def test_the_data_plane_gets_the_openai_error_envelope_for_a_413() -> None:
    """A client SDK parses the error body. A 413 in the gateway's own envelope would
    surface at the caller as an opaque APIStatusError rather than a typed exception."""
    app, _ = limited_app()
    async with limited_client(app) as http:
        response = await http.post(
            "/g/demo/v1/echo",
            content=b"x" * (LIMIT * 2),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["code"] == "payload_too_large"
