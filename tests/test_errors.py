"""Every AppError maps to one status code, and nothing internal leaks to the caller."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
    UpstreamError,
    Validation,
)
from app.main import create_app

ERRORS: dict[str, type[AppError]] = {
    "not_found": NotFound,
    "conflict": Conflict,
    "forbidden": Forbidden,
    "unauthorized": Unauthorized,
    "validation": Validation,
    "upstream": UpstreamError,
    "base": AppError,
}


class Payload(BaseModel):
    count: int


@pytest.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    app = _app_with_failing_routes()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def _app_with_failing_routes() -> FastAPI:
    app = create_app()

    @app.get("/boom/{name}")
    async def boom(name: str) -> None:
        raise ERRORS[name]("something went wrong", details={"field": "value"})

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail: password=hunter2")

    @app.post("/echo")
    async def echo(payload: Payload) -> Payload:
        return payload

    return app


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("conflict", 409, "conflict"),
        ("forbidden", 403, "forbidden"),
        ("unauthorized", 401, "unauthorized"),
        ("validation", 422, "validation_error"),
        ("upstream", 502, "upstream_error"),
        ("base", 500, "internal_error"),
    ],
)
async def test_app_errors_map_to_status_codes(
    error_client: AsyncClient, name: str, status: int, code: str
) -> None:
    response = await error_client.get(f"/boom/{name}")

    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"] == "something went wrong"
    assert error["details"] == {"field": "value"}
    assert error["request_id"] == response.headers["X-Gateway-Request-Id"]


async def test_unexpected_exception_does_not_leak_internals(error_client: AsyncClient) -> None:
    response = await error_client.get("/crash")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "hunter2" not in response.text
    assert error["request_id"]


async def test_request_validation_uses_the_same_envelope(error_client: AsyncClient) -> None:
    response = await error_client.post("/echo", json={"count": "not-a-number"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["errors"][0]["loc"] == ["body", "count"]


async def test_unknown_route_uses_the_same_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
