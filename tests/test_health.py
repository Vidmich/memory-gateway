"""Liveness stays cheap; readiness tells you which dependency is down."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.core.clients import Clients
from app.services import health
from app.services.fact_vectors import MemoryFactVectorStore
from app.services.vector_backends import Backend, VectorBackends
from app.services.vector_binding_store import MemoryVectorBindingStore
from app.services.vector_index import MemoryVectorIndexAdmin
from app.services.vector_store import MemoryVectorStore


async def _ok(_: Clients) -> None:
    return None


async def _reachable() -> None:
    return None


def healthy_backends(*kinds: str, unreachable: str | None = None) -> VectorBackends:
    """A registry of reachable backends, optionally with one that refuses connections.

    Built from the in-memory stores rather than from doubles, so a probe that started
    reaching for something else on a backend would find a real object here.
    """

    async def refused() -> None:
        raise ConnectionRefusedError("connection refused")

    def backend(kind: str) -> Backend:
        vectors = MemoryVectorStore()
        return Backend(
            kind=kind,
            store=vectors,
            facts=MemoryFactVectorStore(),
            admin=MemoryVectorIndexAdmin(vectors),
            ping=refused if kind == unreachable else _reachable,
        )

    named = ("qdrant", *kinds)
    return VectorBackends(
        {kind: backend(kind) for kind in named},
        MemoryVectorBindingStore(),
        default="qdrant",
    )


@pytest.fixture(autouse=True)
def healthy_probes(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every probe passes unless a test overrides one.

    The vector probes come from the registry on ``app.state`` rather than from
    :data:`health.PROBES`, because since task 19 there can be more than one of them and
    which ones exist is a property of the deployment.
    """
    for name in health.PROBES:
        monkeypatch.setitem(health.PROBES, name, _ok)
    monkeypatch.setattr(app.state, "vector_backends", healthy_backends(), raising=False)


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
        "storage": "ok",
        "jobs": "ok",
        "vectors.qdrant": "ok",
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

    monkeypatch.setitem(health.PROBES, "storage", hangs)
    monkeypatch.setattr(
        app.state,
        "settings",
        app.state.settings.model_copy(update={"readiness_timeout_seconds": 0.05}),
    )

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["storage"] == "timeout"


async def test_metrics_endpoint_exposes_build_and_request_metrics(client: AsyncClient) -> None:
    await client.get("/healthz")
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "build_info" in response.text
    assert 'http_requests_total{method="GET",route="/healthz",status="200"}' in response.text


async def test_one_vector_backend_down_does_not_take_the_pod_out_of_rotation(
    client: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule task 19 introduced, and the reason for it.

    An organization on Chroma is affected by Chroma being down; an organization on Qdrant
    is not. Failing readiness would remove this pod from rotation for *both* — which
    reduces capacity for the tenants who are fine and does nothing at all for the ones who
    are not.
    """

    monkeypatch.setattr(
        app.state,
        "vector_backends",
        healthy_backends("chroma", unreachable="chroma"),
        raising=False,
    )

    response = await client.get("/readyz")
    body: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert body["vectors.qdrant"] == "ok"
    assert body["vectors.chroma"] == "error"


async def test_every_vector_backend_down_is_not_ready(
    client: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one backend configured this is exactly the old behaviour: Qdrant down means
    not ready. The disjunction only relaxes things when there is somewhere else to go."""

    monkeypatch.setattr(
        app.state, "vector_backends", healthy_backends(unreachable="qdrant"), raising=False
    )

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["vectors.qdrant"] == "error"
