"""Serving the built SPA from the API process.

The history fallback is the part worth testing: a client-side route like
``/gateways/abc`` exists in the browser's router and nowhere on disk, and a hard refresh
on it has to return ``index.html`` rather than a 404.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app

INDEX = "<!doctype html><title>Memory Gateway</title><div id=root></div>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    return tmp_path


@pytest.fixture
async def served(dist: Path) -> AsyncIterator[AsyncClient]:
    settings = get_settings().model_copy(update={"web_dist_dir": str(dist)})
    application: FastAPI = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def test_the_root_serves_the_app(served: AsyncClient) -> None:
    response = await served.get("/")

    assert response.status_code == 200
    assert "Memory Gateway" in response.text


async def test_an_asset_is_served(served: AsyncClient) -> None:
    response = await served.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.text == "console.log(1)"


async def test_a_client_side_route_falls_back_to_the_app(served: AsyncClient) -> None:
    """A hard refresh on a deep link must not 404."""
    response = await served.get("/gateways/abc")

    assert response.status_code == 200
    assert "Memory Gateway" in response.text


async def test_a_missing_asset_still_404s(served: AsyncClient) -> None:
    """Returning HTML for a missing .js turns a deploy mistake into a confusing
    MIME-type error in the browser console instead of a plain 404."""
    response = await served.get("/assets/index-deadbeef.js")

    assert response.status_code == 404


async def test_the_api_still_wins(served: AsyncClient) -> None:
    """The mount is at "/" and matches everything; it has to be registered last."""
    response = await served.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


async def test_the_data_plane_still_wins(served: AsyncClient) -> None:
    response = await served.post("/g/demo/v1/chat/completions", json={})

    assert response.status_code == 401
    # And in the OpenAI shape, not the control-plane one.
    assert "type" in response.json()["error"]


async def test_health_still_wins(served: AsyncClient) -> None:
    response = await served.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_nothing_is_mounted_without_a_dist_dir() -> None:
    """The default. In development Vite serves the assets and proxies /api here."""
    application = create_app(get_settings().model_copy(update={"web_dist_dir": ""}))

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/")

    assert response.status_code == 404


async def test_a_configured_but_missing_dist_dir_does_not_stop_the_service(
    tmp_path: Path,
) -> None:
    """A bad WEB_DIST_DIR is a broken deploy, but refusing to start would take the data
    plane down with the UI. It logs and serves the API."""
    settings = get_settings().model_copy(update={"web_dist_dir": str(tmp_path / "nope")})
    application = create_app(settings)

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/")).status_code == 404
