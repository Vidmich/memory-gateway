"""Task 103 over the real app: the audit routes, the evaluation routes, and their gates.

The pipeline tests cover the rules; these cover the envelope — status codes, capability
gates, request validation, and the response shapes the SPA reads.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import AuthHarness, DirectoryHarness

HANDBOOK = (
    "Annual leave. Everyone gets twenty-five days of annual leave, plus public holidays, "
    "and can carry five days into the next year with their manager's agreement. Leave is "
    "booked in the portal at least two weeks ahead. Unused leave lapses in December. "
) * 4
QUESTION = "how many days of annual leave can be carried into next year"


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


def headers(auth_harness: AuthHarness, token: str) -> dict[str, str]:
    return auth_harness.bearer(token)


async def indexed_connector(auth_harness: AuthHarness, token: str) -> str:
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    connector_id = str(fixture.connector.id)
    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/upload",
        headers=headers(auth_harness, token),
        files=[("files", ("handbook.md", HANDBOOK.encode(), "text/markdown"))],
    )
    assert response.status_code == 200, response.text
    await fixture.run_jobs()
    return connector_id


async def gateway_reading(auth_harness: AuthHarness, token: str, connector_id: str) -> str:
    response = await auth_harness.client.post(
        "/api/v1/gateways",
        headers=headers(auth_harness, token),
        json={
            "name": "Support",
            "slug": f"support-{uuid.uuid4().hex[:8]}",
            "memory_config": {"connector_ids": [connector_id], "doc_min_score": 0.0},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def run_jobs(auth_harness: AuthHarness) -> None:
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()


# ---------------------------------------------------------------------------
# audits
# ---------------------------------------------------------------------------


async def test_an_audit_is_queued_and_its_report_is_read_back(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)

    empty = await auth_harness.client.get(
        f"/api/v1/connectors/{connector_id}/audits", headers=headers(auth_harness, token)
    )
    assert empty.status_code == 200
    assert empty.json()["chunking"] is None
    assert empty.json()["drift_estimate"]["points"] > 0

    started = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/audits/chunking", headers=headers(auth_harness, token)
    )
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "running"
    assert started.json()["report"] is None
    await run_jobs(auth_harness)

    status = await auth_harness.client.get(
        f"/api/v1/connectors/{connector_id}/audits", headers=headers(auth_harness, token)
    )
    chunking = status.json()["chunking"]
    assert chunking["status"] == "succeeded"
    assert chunking["report"]["kind"] == "chunking"
    assert chunking["report"]["points"] == status.json()["drift_estimate"]["points"]
    assert chunking["report"]["histogram"]["buckets"]
    assert chunking["severity"] in ("green", "amber", "red")


async def test_the_embedding_audit_takes_a_drift_sample_and_the_chunking_one_does_not(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)

    refused = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/audits/chunking",
        headers=headers(auth_harness, token),
        json={"drift_sample": 10},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["param"] == "drift_sample"

    unknown = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/audits/vibes", headers=headers(auth_harness, token)
    )
    assert unknown.status_code == 422

    started = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/audits/embedding",
        headers=headers(auth_harness, token),
        json={"drift_sample": 5},
    )
    assert started.status_code == 202, started.text
    assert started.json()["drift_sample"] == 5
    await run_jobs(auth_harness)

    status = await auth_harness.client.get(
        f"/api/v1/connectors/{connector_id}/audits", headers=headers(auth_harness, token)
    )
    embedding = status.json()["embedding"]
    assert embedding["report"]["kind"] == "embedding"
    assert embedding["report"]["drift"]["shape"] == "healthy"
    assert embedding["report"]["scanned"] > 0
    # One document of one chunk has no neighbour of its own to agree with: not measured,
    # rather than measured as zero.
    assert embedding["report"]["agreement"] is None


async def test_alerts_list_only_red_findings(auth_harness: AuthHarness, token: str) -> None:
    response = await auth_harness.client.get(
        "/api/v1/validation/alerts", headers=headers(auth_harness, token)
    )
    assert response.status_code == 200
    assert response.json() == {"items": []}


# ---------------------------------------------------------------------------
# evaluation sets, items, runs
# ---------------------------------------------------------------------------


async def test_the_whole_evaluation_flow_over_http(auth_harness: AuthHarness, token: str) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    gateway_id = await gateway_reading(auth_harness, token, connector_id)
    auth = headers(auth_harness, token)

    created = await auth_harness.client.post(
        f"/api/v1/gateways/{gateway_id}/evaluation-sets",
        headers=auth,
        json={"name": "Handbook questions", "description": "From support."},
    )
    assert created.status_code == 201, created.text
    set_id = created.json()["id"]
    assert created.json()["counts"] == {"total": 0, "verified": 0, "generated": 0, "negatives": 0}

    # Label the way the screen does: Try retrieval, then add what came back.
    preview = await auth_harness.client.post(
        f"/api/v1/gateways/{gateway_id}/try-retrieval", headers=auth, json={"query": QUESTION}
    )
    assert preview.status_code == 200, preview.text
    top = preview.json()["chunks"][0]
    added = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/items",
        headers=auth,
        json={
            "question": QUESTION,
            "relevant": [{"chunk_id": top["id"], "document_id": top["document_id"]}],
        },
    )
    assert added.status_code == 201, added.text
    item = added.json()
    assert item["source"] == "manual" and item["verified"] is True
    assert item["relevant"][0]["text"]
    assert item["negative"] is False

    negative = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/items",
        headers=auth,
        json={"question": "what is the wifi password", "verified": False},
    )
    assert negative.status_code == 201
    assert negative.json()["negative"] is True

    patched = await auth_harness.client.patch(
        f"/api/v1/evaluation-items/{negative.json()['id']}",
        headers=auth,
        json={"verified": True, "notes": "Checked with IT."},
    )
    assert patched.status_code == 200
    assert patched.json()["verified"] is True and patched.json()["notes"] == "Checked with IT."

    listed = await auth_harness.client.get(
        f"/api/v1/gateways/{gateway_id}/evaluation-sets", headers=auth
    )
    [view] = listed.json()["items"]
    assert view["counts"]["total"] == 2 and view["counts"]["negatives"] == 1
    assert view["last_run"] is None

    queued = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/runs",
        headers=auth,
        json={"memory_config": {"doc_top_k": 3}},
    )
    assert queued.status_code == 202, queued.text
    run_id = queued.json()["id"]
    assert queued.json()["status"] == "queued"
    assert queued.json()["total_items"] == 2
    await run_jobs(auth_harness)

    run = await auth_harness.client.get(f"/api/v1/evaluation-runs/{run_id}", headers=auth)
    assert run.status_code == 200
    body = run.json()
    assert body["status"] == "succeeded", body["error"]
    assert body["metrics"]["all"]["chunk"]["recall"] == 1.0
    assert body["metrics"]["all"]["negatives"] == 1
    assert body["patch"] == {"doc_top_k": 3}
    assert len(body["results"]) == 2
    assert body["snapshot"]["connectors"][connector_id]["fingerprints"]

    runs = await auth_harness.client.get(f"/api/v1/evaluation-sets/{set_id}/runs", headers=auth)
    assert [entry["id"] for entry in runs.json()["items"]] == [run_id]
    assert "results" not in runs.json()["items"][0]

    second = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/runs", headers=auth, json={}
    )
    assert second.status_code == 202
    await run_jobs(auth_harness)
    diff = await auth_harness.client.get(
        f"/api/v1/evaluation-runs/{second.json()['id']}/diff/{run_id}", headers=auth
    )
    assert diff.status_code == 200, diff.text
    assert diff.json()["before"]["id"] == run_id
    assert diff.json()["config_changes"] == {"doc_top_k": [3, 6]}
    assert any(delta["name"] == "chunk recall" for delta in diff.json()["metrics"])

    detail = await auth_harness.client.get(f"/api/v1/evaluation-sets/{set_id}", headers=auth)
    assert detail.status_code == 200
    assert len(detail.json()["items"]) == 2

    removed = await auth_harness.client.delete(f"/api/v1/evaluation-sets/{set_id}", headers=auth)
    assert removed.status_code == 204
    gone = await auth_harness.client.get(f"/api/v1/evaluation-runs/{run_id}", headers=auth)
    assert gone.status_code == 404


async def test_an_import_reads_the_gateways_window(auth_harness: AuthHarness, token: str) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    gateway_id = await gateway_reading(auth_harness, token, connector_id)
    auth = headers(auth_harness, token)
    created = await auth_harness.client.post(
        f"/api/v1/gateways/{gateway_id}/evaluation-sets", headers=auth, json={"name": "Imported"}
    )
    set_id = created.json()["id"]

    imported = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/import",
        headers=auth,
        json={"from": "2026-01-01T00:00:00Z", "to": "2026-01-08T00:00:00Z"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"imported": 0, "duplicates": 0, "skipped": 0, "labelled": 0}

    backwards = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/import",
        headers=auth,
        json={"from": "2026-01-08T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    )
    assert backwards.status_code == 422


async def test_request_validation_speaks_the_field_it_refused(
    auth_harness: AuthHarness, token: str
) -> None:
    connector_id = await indexed_connector(auth_harness, token)
    gateway_id = await gateway_reading(auth_harness, token, connector_id)
    auth = headers(auth_harness, token)
    created = await auth_harness.client.post(
        f"/api/v1/gateways/{gateway_id}/evaluation-sets", headers=auth, json={"name": "  "}
    )
    assert created.status_code == 422

    created = await auth_harness.client.post(
        f"/api/v1/gateways/{gateway_id}/evaluation-sets", headers=auth, json={"name": "Ok"}
    )
    set_id = created.json()["id"]
    missing_document = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/items",
        headers=auth,
        json={"question": "q?", "relevant": [{"chunk_id": "abc"}]},
    )
    assert missing_document.status_code == 422

    empty_run = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/runs", headers=auth
    )
    assert empty_run.status_code == 422

    generate_without_model = await auth_harness.client.post(
        f"/api/v1/evaluation-sets/{set_id}/generate", headers=auth, json={"count": 99}
    )
    assert generate_without_model.status_code == 422


async def test_a_viewer_may_read_validation_and_not_start_anything(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    viewer = world.acme_viewer
    connector_id = world.acme_connector.id
    gateway_id = world.acme_gateway.id

    listed = await directory.as_user(viewer, "GET", f"/api/v1/connectors/{connector_id}/audits")
    assert listed.status_code == 200
    sets = await directory.as_user(viewer, "GET", f"/api/v1/gateways/{gateway_id}/evaluation-sets")
    assert sets.status_code == 200
    alerts = await directory.as_user(viewer, "GET", "/api/v1/validation/alerts")
    assert alerts.status_code == 200

    for method, path, body in (
        ("POST", f"/api/v1/connectors/{connector_id}/audits/chunking", None),
        ("POST", f"/api/v1/gateways/{gateway_id}/evaluation-sets", {"name": "Nope"}),
    ):
        response = await directory.as_user(viewer, method, path, json_body=body)
        assert response.status_code == 403, (method, path, response.text)
