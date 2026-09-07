"""Liveness stays cheap; readiness tells you which dependency is down."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.clients import Clients
from app.services import health


async def _ok(_: Clients) -> None:
    return None


@pytest.fixture(autouse=True)
def healthy_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every probe passes unless a test overrides one."""
    for name in health.PROBES:
        monkeypatch.setitem(health.PROBES, name, _ok)


async def test_healthz_is_liveness_only(
    client: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(_: Clients) -> None:
        raise AssertionError("liveness must not touch dependencies")

    for name in health.PROBES:
        monkeypatch.setitem(health.PROBES, name, never)

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": app.state.settings.version}


async def test_readyz_reports_every_dependency(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "postgres": "ok",
        "redis": "ok",
        "qdrant": "ok",
        "storage": "ok",
        "jobs": "ok",
    }


async def test_readyz_names_the_failing_dependency(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_: Clients) -> None:
        raise ConnectionRefusedError("connection refused to postgres:5432")

    monkeypatch.setitem(health.PROBES, "postgres", broken)

    response = await client.get("/readyz")
    body: dict[str, Any] = response.json()

    assert response.status_code == 503
    assert body["postgres"] == "error"
    assert body["redis"] == "ok"
    assert "connection refused" in body["detail"]["postgres"]


async def test_readyz_times_out_instead_of_hanging(
    client: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hangs(_: Clients) -> None:
        await asyncio.sleep(30)

    monkeypatch.setitem(health.PROBES, "qdrant", hangs)
    monkeypatch.setattr(
        app.state,
        "settings",
        app.state.settings.model_copy(update={"readiness_timeout_seconds": 0.05}),
    )

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["qdrant"] == "timeout"


async def test_metrics_endpoint_exposes_build_and_request_metrics(client: AsyncClient) -> None:
    await client.get("/healthz")
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "build_info" in response.text
    assert 'http_requests_total{method="GET",route="/healthz",status="200"}' in response.text
