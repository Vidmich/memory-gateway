"""Task 102 over the real app: the connector's summarization section, the document's summary
routes, the organization's default model, and the Monitoring panel's numbers.

The pipeline tests cover the rules; these cover the envelope — status codes, capability
gates, and the response shapes the SPA reads.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import AuthHarness

HANDBOOK = b"# Travel policy\n\n" + b"\n\n".join(
    b"Section %d. " % index
    + b" ".join(b"Rule %d.%d applies to travel expenses." % (index, j) for j in range(12))
    for index in range(8)
)
SUMMARY = "The travel policy: approvals, thresholds and receipts."


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


def arm(auth_harness: AuthHarness, *replies: str | Exception) -> None:
    """Give the harness's pipeline a model to call: the scripted one, and the catalog row
    the resolver's platform default points at."""
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    fixture.database.add_model(fixture.summary_row)
    fixture.summary_model.replies[:] = list(replies) or [SUMMARY]


async def connector_with(
    auth_harness: AuthHarness, token: str, summarization: dict[str, Any]
) -> dict[str, Any]:
    response = await auth_harness.client.post(
        "/api/v1/connectors",
        headers=auth_harness.bearer(token),
        json={"name": "Policies", "summarization": summarization},
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def indexed(auth_harness: AuthHarness, token: str, connector_id: str) -> dict[str, Any]:
    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector_id}/upload",
        headers=auth_harness.bearer(token),
        files=[("files", ("handbook.md", HANDBOOK, "text/markdown"))],
    )
    assert response.status_code == 200, response.text
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()
    listing = await auth_harness.client.get(
        f"/api/v1/connectors/{connector_id}/documents", headers=auth_harness.bearer(token)
    )
    [document] = listing.json()["items"]
    result: dict[str, Any] = document
    return result


# ---------------------------------------------------------------------------
# the connector's section
# ---------------------------------------------------------------------------


async def test_a_connector_carries_its_summarization_settings_and_the_model_it_resolves_to(
    auth_harness: AuthHarness, token: str
) -> None:
    arm(auth_harness)
    body = await connector_with(auth_harness, token, {"mode": "summary_chunk"})

    assert body["summarization"]["mode"] == "summary_chunk"
    assert body["summarization"]["max_summary_tokens"] == 150
    assert body["effective_summarization"]["markdown"]["mode"] == "summary_chunk"
    assert body["summary_model"] == {
        "id": str(auth_harness.auth.connectors.summary_row.id),  # type: ignore[union-attr]
        "name": "cheap-summarizer",
        "inherited": True,
    }
    assert body["reindex_required"] is False


async def test_a_misspelled_mode_and_a_null_section_are_refused(
    auth_harness: AuthHarness, token: str
) -> None:
    body = await connector_with(auth_harness, token, {})

    wrong = await auth_harness.client.patch(
        f"/api/v1/connectors/{body['id']}",
        headers=auth_harness.bearer(token),
        json={"summarization": {"mode": "sometimes"}},
    )
    assert wrong.status_code == 422
    assert "summarization.mode" in wrong.text

    null = await auth_harness.client.patch(
        f"/api/v1/connectors/{body['id']}",
        headers=auth_harness.bearer(token),
        json={"summarization": None},
    )
    assert null.status_code == 422


async def test_turning_contextual_on_says_every_document_must_be_re_embedded(
    auth_harness: AuthHarness, token: str
) -> None:
    arm(auth_harness)
    body = await connector_with(auth_harness, token, {})
    await indexed(auth_harness, token, body["id"])

    response = await auth_harness.client.patch(
        f"/api/v1/connectors/{body['id']}",
        headers=auth_harness.bearer(token),
        json={"summarization": {"mode": "contextual"}},
    )

    assert response.status_code == 200
    assert response.json()["reindex_required"] is True
    assert "markdown" in response.json()["reindex_formats"]
    listing = await auth_harness.client.get(
        f"/api/v1/connectors/{body['id']}/documents", headers=auth_harness.bearer(token)
    )
    [document] = listing.json()["items"]
    assert document["stale"] is True


# ---------------------------------------------------------------------------
# the document's summary
# ---------------------------------------------------------------------------


async def test_a_document_row_carries_its_summary_and_the_inspector_labels_the_point(
    auth_harness: AuthHarness, token: str
) -> None:
    arm(auth_harness)
    body = await connector_with(auth_harness, token, {"mode": "both"})
    document = await indexed(auth_harness, token, body["id"])

    assert document["summary"] == SUMMARY
    assert document["summary_status"] == "summarized"
    assert document["summary_model"] == "cheap-summarizer"
    assert document["summary_tokens_in"] == 120
    assert document["summarized_at"] is not None

    chunks = await auth_harness.client.get(
        f"/api/v1/documents/{document['id']}/chunks", headers=auth_harness.bearer(token)
    )
    items = chunks.json()["chunks"]
    kinds = {item["kind"] for item in items}
    assert kinds == {"summary", "source"}
    summary = next(item for item in items if item["kind"] == "summary")
    assert summary["text"] == SUMMARY
    assert summary["page_or_section"] == "Summary"
    source = next(item for item in items if item["kind"] == "source")
    assert source["context"] == SUMMARY
    assert source["embedded_because"] == "context"
    assert chunks.json()["chunk_count"] == len(items) - 1


async def test_editing_and_regenerating_a_summary(auth_harness: AuthHarness, token: str) -> None:
    arm(auth_harness, SUMMARY, "Regenerated.")
    body = await connector_with(auth_harness, token, {"mode": "summary_chunk"})
    document = await indexed(auth_harness, token, body["id"])
    fixture = auth_harness.auth.connectors
    assert fixture is not None

    edited = await auth_harness.client.patch(
        f"/api/v1/documents/{document['id']}/summary",
        headers=auth_harness.bearer(token),
        json={"summary": "By hand."},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["summary"] == "By hand."
    assert edited.json()["summary_model"] == "manual"
    await fixture.run_jobs()

    regenerated = await auth_harness.client.post(
        f"/api/v1/documents/{document['id']}/summarize", headers=auth_harness.bearer(token)
    )
    assert regenerated.status_code == 200, regenerated.text
    await fixture.run_jobs()
    listing = await auth_harness.client.get(
        f"/api/v1/connectors/{body['id']}/documents", headers=auth_harness.bearer(token)
    )
    [after] = listing.json()["items"]
    assert after["summary"] == "Regenerated."
    assert after["summary_model"] == "cheap-summarizer"


async def test_an_empty_summary_and_an_unknown_document_are_refused(
    auth_harness: AuthHarness, token: str
) -> None:
    blank = await auth_harness.client.patch(
        "/api/v1/documents/00000000-0000-4000-8000-000000000001/summary",
        headers=auth_harness.bearer(token),
        json={"summary": ""},
    )
    assert blank.status_code == 422

    missing = await auth_harness.client.patch(
        "/api/v1/documents/00000000-0000-4000-8000-000000000001/summary",
        headers=auth_harness.bearer(token),
        json={"summary": "x"},
    )
    assert missing.status_code == 404


async def test_a_failed_summary_is_on_the_row_with_its_reason(
    auth_harness: AuthHarness, token: str
) -> None:
    from app.api.proxy.errors import UpstreamStatus

    arm(auth_harness, UpstreamStatus(status_code=400, model_name="cheap-summarizer", message="no"))
    body = await connector_with(auth_harness, token, {"mode": "summary_chunk"})
    document = await indexed(auth_harness, token, body["id"])

    assert document["status"] == "indexed"
    assert document["summary_status"] == "failed"
    assert "cheap-summarizer" in document["summary_error"]


# ---------------------------------------------------------------------------
# the organization's default
# ---------------------------------------------------------------------------


async def test_the_default_model_reads_and_writes_and_says_where_it_came_from(
    auth_harness: AuthHarness, token: str
) -> None:
    arm(auth_harness)
    fixture = auth_harness.auth.connectors
    assert fixture is not None

    before = await auth_harness.client.get(
        "/api/v1/summarization", headers=auth_harness.bearer(token)
    )
    assert before.status_code == 200
    assert before.json()["config"]["model_id"] is None
    assert before.json()["effective_model_name"] == "cheap-summarizer"
    assert before.json()["effective_model_source"] == "platform"

    chosen = await auth_harness.client.patch(
        "/api/v1/summarization",
        headers=auth_harness.bearer(token),
        json={"model_id": str(fixture.summary_row.id)},
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["config"]["model_id"] == str(fixture.summary_row.id)
    assert chosen.json()["effective_model_source"] == "summarization"

    cleared = await auth_harness.client.patch(
        "/api/v1/summarization", headers=auth_harness.bearer(token), json={"model_id": None}
    )
    assert cleared.json()["config"]["model_id"] is None
    assert cleared.json()["effective_model_source"] == "platform"

    unknown = await auth_harness.client.patch(
        "/api/v1/summarization", headers=auth_harness.bearer(token), json={"mode": "both"}
    )
    assert unknown.status_code == 422


# ---------------------------------------------------------------------------
# the panel
# ---------------------------------------------------------------------------


async def test_the_health_endpoint_sums_the_ledger_over_the_window(
    auth_harness: AuthHarness, token: str
) -> None:
    arm(auth_harness)
    body = await connector_with(auth_harness, token, {"mode": "summary_chunk"})
    await indexed(auth_harness, token, body["id"])

    response = await auth_harness.client.get(
        "/api/v1/summarization/health", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200, response.text
    health = response.json()
    assert health["runs"] == 1
    assert health["documents"] == 1
    assert health["tokens_in"] == 120
    assert health["tokens_out"] == 40
    assert health["failure_rate"] == 0
    [day] = health["days"]
    assert day["documents"] == 1
    [model] = health["by_model"]
    assert model["model_name"] == "cheap-summarizer"
    [top] = health["top_connectors"]
    assert top["connector_id"] == body["id"]
    assert top["name"] == "Policies"
    assert health["waiting"] == []
    assert health["waiting_documents"] == 0

    narrowed = await auth_harness.client.get(
        "/api/v1/summarization/health",
        headers=auth_harness.bearer(token),
        params={"connector_id": "00000000-0000-4000-8000-000000000001"},
    )
    assert narrowed.json()["runs"] == 0

    backwards = await auth_harness.client.get(
        "/api/v1/summarization/health",
        headers=auth_harness.bearer(token),
        params={"from": "2026-09-02T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
    )
    assert backwards.status_code == 422


async def test_the_panel_counts_documents_waiting_on_the_cap(
    auth_harness: AuthHarness, token: str
) -> None:
    arm(auth_harness)
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    body = await connector_with(
        auth_harness, token, {"mode": "contextual", "daily_document_cap": 1}
    )
    response = await auth_harness.client.post(
        f"/api/v1/connectors/{body['id']}/upload",
        headers=auth_harness.bearer(token),
        files=[
            ("files", ("one.md", HANDBOOK, "text/markdown")),
            ("files", ("two.md", HANDBOOK, "text/markdown")),
        ],
    )
    assert response.status_code == 200
    await fixture.run_jobs_until_parked()

    health = (
        await auth_harness.client.get(
            "/api/v1/summarization/health", headers=auth_harness.bearer(token)
        )
    ).json()

    assert health["capped"] == 1
    assert health["waiting_documents"] == 1
    assert health["waiting"] == [{"connector_id": body["id"], "name": "Policies", "documents": 1}]
    listing = await auth_harness.client.get(
        f"/api/v1/connectors/{body['id']}/documents", headers=auth_harness.bearer(token)
    )
    parked = next(d for d in listing.json()["items"] if d["status"] == "pending")
    assert parked["reason"] == "summarization_cap"
