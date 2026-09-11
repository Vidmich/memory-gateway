"""Task 104 over the real app: the reprocess routes, the two-axis document listing, the
connector's stale summary, and the gateway's notice.

The pipeline tests cover the rules; these cover the envelope — status codes, capability
gates, request validation, and the response shapes the SPA reads.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import AuthHarness, DirectoryHarness

HANDBOOK = (
    "Annual leave. Everyone gets twenty-five days of annual leave, plus public holidays, "
    "and can carry five days into the next year with their manager's agreement. Leave is "
    "booked in the portal at least two weeks ahead. Unused leave lapses in December. "
) * 6


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


def headers(auth_harness: AuthHarness, token: str) -> dict[str, str]:
    return auth_harness.bearer(token)


async def indexed_connector(auth_harness: AuthHarness, token: str, *names: str) -> str:
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    connector_id = str(fixture.connector.id)
    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/upload",
        headers=headers(auth_harness, token),
        files=[
            ("files", (name, HANDBOOK.encode(), "text/markdown"))
            for name in (names or ("handbook.md",))
        ],
    )
    assert response.status_code == 200, response.text
    await fixture.run_jobs()
    return connector_id


async def run_jobs(auth_harness: AuthHarness) -> None:
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()


async def change_chunking(auth_harness: AuthHarness, token: str, connector_id: str) -> Any:
    response = await auth_harness.client.patch(
        f"/api/v1/connectors/{connector_id}",
        headers=headers(auth_harness, token),
        json={"chunking": {"chunk_size": 400, "overlap": 40}},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_whole_flow_over_http(auth_harness: AuthHarness, token: str) -> None:
    """Save → stale everywhere → reprocess → progress → current everywhere → history."""
    connector_id = await indexed_connector(auth_harness, token, "a.md", "b.md")
    client, auth = auth_harness.client, headers(auth_harness, token)

    patched = await change_chunking(auth_harness, token, connector_id)
    assert patched["reindex_required"] is True and patched["reindex_formats"] == ["markdown"]
    assert patched["stale_documents"] == 2 and patched["reprocessing"] is None
    assert set(patched["effective_fingerprints"]) >= {"markdown", "pdf", "code"}

    documents = await client.get(
        f"/api/v1/connectors/{connector_id}/documents?index_status=stale", headers=auth
    )
    rows = documents.json()["items"]
    assert len(rows) == 2
    assert {row["index_status"] for row in rows} == {"stale"}
    assert {row["stale"] for row in rows} == {True}
    assert {row["stale_reason"] for row in rows} == {"chunking"}
    assert all("settings changed" in row["stale_detail"] for row in rows)
    assert all(row["index_fingerprint"] for row in rows)

    started = await client.post(f"/api/v1/connectors/{connector_id}/reprocess", headers=auth)
    assert started.status_code == 202, started.text
    run = started.json()
    assert run["created"] is True and run["scope"] == "stale" and run["trigger"] == "chunking"
    assert run["total"] == 2 and run["status"] == "running"
    assert run["progress"] == {"done": 0, "total": 2, "fraction": 0.0, "eta_seconds": None}

    again = await client.post(
        f"/api/v1/connectors/{connector_id}/reprocess", headers=auth, json={"scope": "all"}
    )
    assert again.status_code == 202 and again.json()["created"] is False
    assert again.json()["id"] == run["id"]

    header = await client.get(f"/api/v1/connectors/{connector_id}", headers=auth)
    assert header.json()["reprocessing"]["id"] == run["id"]
    assert header.json()["reprocessing_documents"] == 2 and header.json()["stale_documents"] == 0
    busy = await client.get(
        f"/api/v1/connectors/{connector_id}/documents?index_status=reprocessing", headers=auth
    )
    assert len(busy.json()["items"]) == 2

    await run_jobs(auth_harness)

    finished = await client.get(f"/api/v1/reprocessing-runs/{run['id']}", headers=auth)
    assert finished.status_code == 200
    body = finished.json()
    assert body["status"] == "succeeded" and body["done"] == 2 and body["finished_at"]
    assert body["progress"]["fraction"] == 1.0 and body["spent_tokens"] > 0
    after = await client.get(f"/api/v1/connectors/{connector_id}", headers=auth)
    assert after.json()["reindex_required"] is False and after.json()["stale_documents"] == 0
    assert after.json()["reprocessing"] is None
    history = await client.get(f"/api/v1/connectors/{connector_id}/reprocessing-runs", headers=auth)
    assert [row["id"] for row in history.json()["items"]] == [run["id"]]
    assert history.json()["items"][0]["requested_by_label"]


async def test_the_reindex_alias_returns_a_count_and_starts_a_tracked_run(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    client, auth = auth_harness.client, headers(auth_harness, token)

    response = await client.post(
        f"/api/v1/connectors/{connector_id}/reindex", headers=auth, json={"formats": ["markdown"]}
    )

    assert response.status_code == 200 and response.json() == {"documents": 1}
    history = await client.get(f"/api/v1/connectors/{connector_id}/reprocessing-runs", headers=auth)
    [run] = history.json()["items"]
    assert (
        run["scope"] == "formats" and run["formats"] == ["markdown"] and run["trigger"] == "manual"
    )


async def test_retry_wants_a_finished_run_and_the_preview_writes_nothing(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    client, auth = auth_harness.client, headers(auth_harness, token)

    preview = await client.post(
        f"/api/v1/connectors/{connector_id}/stale-preview",
        headers=auth,
        json={"chunking": {"chunk_size": 400, "overlap": 40}},
    )
    assert preview.status_code == 200
    assert preview.json() == {"formats": {"markdown": 1}, "total": 1}
    nothing = await client.post(
        f"/api/v1/connectors/{connector_id}/stale-preview",
        headers=auth,
        json={"description": "renamed"},
    )
    assert nothing.json() == {"formats": {}, "total": 0}
    untouched = await client.get(f"/api/v1/connectors/{connector_id}", headers=auth)
    assert untouched.json()["stale_documents"] == 0

    await change_chunking(auth_harness, token, connector_id)
    started = await client.post(f"/api/v1/connectors/{connector_id}/reprocess", headers=auth)
    running = started.json()["id"]
    refused = await client.post(f"/api/v1/reprocessing-runs/{running}/retry", headers=auth)
    assert refused.status_code == 422
    await run_jobs(auth_harness)
    retried = await client.post(f"/api/v1/reprocessing-runs/{running}/retry", headers=auth)
    assert retried.status_code == 202
    assert retried.json()["scope"] == "failed" and retried.json()["total"] == 0
    assert retried.json()["status"] == "succeeded"


async def test_request_validation_speaks_the_field_it_refused(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    client, auth = auth_harness.client, headers(auth_harness, token)

    bad_scope = await client.post(
        f"/api/v1/connectors/{connector_id}/reprocess", headers=auth, json={"scope": "everything"}
    )
    assert bad_scope.status_code == 422
    assert "scope" in bad_scope.text
    bad_format = await client.post(
        f"/api/v1/connectors/{connector_id}/reprocess",
        headers=auth,
        json={"scope": "formats", "formats": ["parchment"]},
    )
    assert bad_format.status_code == 422
    no_format = await client.post(
        f"/api/v1/connectors/{connector_id}/reprocess", headers=auth, json={"scope": "formats"}
    )
    assert no_format.status_code == 422
    bad_filter = await client.get(
        f"/api/v1/connectors/{connector_id}/documents?index_status=confused", headers=auth
    )
    assert bad_filter.status_code == 422
    assert bad_filter.json()["error"]["param"] == "index_status"
    missing = await client.get(f"/api/v1/reprocessing-runs/{uuid.uuid4()}", headers=auth)
    assert missing.status_code == 404


async def test_a_gateway_reading_a_stale_connector_says_so_and_a_current_one_does_not(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    client, auth = auth_harness.client, headers(auth_harness, token)
    created = await client.post(
        "/api/v1/gateways",
        headers=auth,
        json={
            "name": "Support",
            "slug": f"support-{uuid.uuid4().hex[:8]}",
            "memory_config": {"connector_ids": [connector_id]},
        },
    )
    assert created.status_code == 201, created.text
    gateway_id = created.json()["id"]
    assert created.json()["stale_connectors"] == []

    await change_chunking(auth_harness, token, connector_id)

    fetched = await client.get(f"/api/v1/gateways/{gateway_id}", headers=auth)
    [notice] = fetched.json()["stale_connectors"]
    assert notice["id"] == connector_id and notice["stale"] == 1 and notice["reprocessing"] == 0
    assert notice["name"]

    await client.post(f"/api/v1/connectors/{connector_id}/reprocess", headers=auth)
    await run_jobs(auth_harness)
    fetched = await client.get(f"/api/v1/gateways/{gateway_id}", headers=auth)
    assert fetched.json()["stale_connectors"] == []


async def test_the_alerts_route_lists_connectors_stale_past_the_threshold(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    client, auth = auth_harness.client, headers(auth_harness, token)
    await change_chunking(auth_harness, token, connector_id)

    quiet = await client.get("/api/v1/reprocessing/alerts", headers=auth)
    loud = await client.get("/api/v1/reprocessing/alerts?older_than_hours=0", headers=auth)

    assert quiet.status_code == 200 and quiet.json() == {"items": []}
    [alert] = loud.json()["items"]
    assert alert["connector_id"] == connector_id and alert["stale_documents"] == 1
    assert alert["connector_name"] and alert["stale_since"] and alert["age_hours"] >= 0


async def test_a_viewer_may_read_runs_and_not_start_one(directory: DirectoryHarness) -> None:
    world = directory.world
    viewer = world.acme_viewer
    connector_id = world.acme_connector.id

    listed = await directory.as_user(
        viewer, "GET", f"/api/v1/connectors/{connector_id}/reprocessing-runs"
    )
    assert listed.status_code == 200
    preview = await directory.as_user(
        viewer,
        "POST",
        f"/api/v1/connectors/{connector_id}/stale-preview",
        json_body={"chunking": {"chunk_size": 400}},
    )
    assert preview.status_code == 200, "a preview is a read"

    for method, path, body in (
        ("POST", f"/api/v1/connectors/{connector_id}/reprocess", None),
        ("POST", f"/api/v1/reprocessing-runs/{uuid.uuid4()}/retry", None),
    ):
        response = await directory.as_user(viewer, method, path, json_body=body)
        assert response.status_code == 403, (method, path, response.text)
