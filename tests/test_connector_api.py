"""``/api/v1/connectors`` over the real app.

The service tests cover the rules; these cover the *envelope* — status codes, capability
gates, and the two response shapes that are unusual enough to be worth pinning.

``POST /upload`` returns 200 with a per-file outcome list even when a file was rejected,
and that is the shape most likely to be "simplified" later into a 4xx. A test that reads
like a contract is the cheapest way to stop that.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import AuthHarness

HANDBOOK = b"# Handbook\n\nAcme pays for widgets and gizmos.\n"
MOV = b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 9000


#: The harness seeds one connector so the cross-tenant net has a real foreign id to aim
#: at. These tests create their own with a different name rather than reusing it, so a
#: name-collision assertion elsewhere cannot make this file fail for an unrelated reason.
NAME = "Engineering docs"


async def created(harness: AuthHarness, token: str, **body: Any) -> dict[str, Any]:
    response = await harness.client.post(
        "/api/v1/connectors",
        headers=harness.bearer(token),
        json={"name": NAME, **body},
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


@pytest.fixture
async def token(auth_harness: AuthHarness) -> str:
    return await auth_harness.sign_in()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_creating_a_connector_answers_201_with_the_full_configuration(
    auth_harness: AuthHarness, token: str
) -> None:
    body = await created(auth_harness, token)

    assert body["name"] == NAME
    assert body["type"] == "managed_file_drop"
    assert body["status"] == "ready"
    assert body["chunking"]["strategy"] == "recursive"
    assert body["chunking"]["chunk_size"] == 1000
    assert body["storage_prefix"].startswith("orgs/")


async def test_an_unknown_field_is_refused(auth_harness: AuthHarness, token: str) -> None:
    """``extra="forbid"``. A caller who sent ``storage_prefix`` is told, rather than
    quietly getting a prefix they did not ask for."""
    response = await auth_harness.client.post(
        "/api/v1/connectors",
        headers=auth_harness.bearer(token),
        json={"name": "Docs", "storage_prefix": "orgs/somebody-else/"},
    )

    assert response.status_code == 422


async def test_an_unknown_connector_type_is_refused(auth_harness: AuthHarness, token: str) -> None:
    response = await auth_harness.client.post(
        "/api/v1/connectors",
        headers=auth_harness.bearer(token),
        json={"name": "Warehouse", "type": "sql"},
    )

    assert response.status_code == 422


async def test_listing_connectors_returns_a_page(auth_harness: AuthHarness, token: str) -> None:
    await created(auth_harness, token)

    response = await auth_harness.client.get(
        "/api/v1/connectors", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert NAME in [item["name"] for item in body["items"]]
    assert body["next_cursor"] is None


async def test_updating_the_chunking_reports_whether_a_reindex_is_needed(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)

    response = await auth_harness.client.patch(
        f"/api/v1/connectors/{connector['id']}",
        headers=auth_harness.bearer(token),
        json={"chunking": {"strategy": "by_heading"}},
    )

    assert response.status_code == 200
    assert response.json()["chunking"]["strategy"] == "by_heading"
    # Nothing is indexed, so there is nothing to reindex and no banner to show.
    assert response.json()["reindex_required"] is False


async def test_an_impossible_chunking_combination_is_a_422(
    auth_harness: AuthHarness, token: str
) -> None:
    """Overlap at or above the chunk size is not a slow configuration, it is a
    non-terminating one."""
    connector = await created(auth_harness, token)

    response = await auth_harness.client.patch(
        f"/api/v1/connectors/{connector['id']}",
        headers=auth_harness.bearer(token),
        json={"chunking": {"chunk_size": 100, "overlap": 90}},
    )

    assert response.status_code == 422
    assert "half" in response.text


async def test_a_null_name_is_refused_rather_than_ignored(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)

    response = await auth_harness.client.patch(
        f"/api/v1/connectors/{connector['id']}",
        headers=auth_harness.bearer(token),
        json={"name": None},
    )

    assert response.status_code == 422


async def test_deleting_a_connector_answers_202(auth_harness: AuthHarness, token: str) -> None:
    """202, not 204. The row is marked ``deleting`` immediately and the objects go in a
    job; pretending otherwise would show a connector as gone while it is visibly there."""
    connector = await created(auth_harness, token)

    response = await auth_harness.client.delete(
        f"/api/v1/connectors/{connector['id']}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 202

    after = await auth_harness.client.get(
        f"/api/v1/connectors/{connector['id']}", headers=auth_harness.bearer(token)
    )
    assert after.json()["status"] == "deleting"


async def test_a_connector_that_does_not_exist_is_a_404(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.get(
        f"/api/v1/connectors/{uuid.uuid4()}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


async def upload(
    client: AsyncClient, headers: dict[str, str], connector_id: str, *files: tuple[str, bytes]
) -> Any:
    return await client.post(
        f"/api/v1/connectors/{connector_id}/upload",
        headers=headers,
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )


async def test_uploading_files_answers_200_with_one_outcome_each(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)

    response = await upload(
        auth_harness.client,
        auth_harness.bearer(token),
        connector["id"],
        ("handbook.md", HANDBOOK),
        ("notes.md", b"# Notes\n\nplovers\n"),
    )

    assert response.status_code == 200
    files = response.json()["files"]
    assert [file["filename"] for file in files] == ["handbook.md", "notes.md"]
    assert all(file["status"] == "pending" for file in files)
    assert all(file["document_id"] for file in files)


async def test_a_rejected_file_is_reported_in_the_body_not_the_status(
    auth_harness: AuthHarness, token: str
) -> None:
    """The shape most likely to be "simplified" into a 4xx later. There is no status code
    that means "thirty-nine worked and one did not"."""
    connector = await created(auth_harness, token)

    response = await upload(
        auth_harness.client,
        auth_harness.bearer(token),
        connector["id"],
        ("good.md", HANDBOOK),
        # A name that reduces to nothing after the path segments that could escape the
        # prefix are removed. Starlette rejects a *blank* filename before any of our code
        # runs, so this is the closest a client can get to sending one.
        ("..", b"nameless"),
    )

    assert response.status_code == 200
    files = response.json()["files"]
    assert files[0]["status"] == "pending"
    assert files[1]["status"] == "rejected"
    assert files[1]["error"]


async def test_uploading_to_a_connector_that_does_not_exist_is_a_404(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await upload(
        auth_harness.client, auth_harness.bearer(token), str(uuid.uuid4()), ("a.md", HANDBOOK)
    )

    assert response.status_code == 404


async def test_a_presigned_upload_url_names_a_key_and_a_lifetime(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/upload-url",
        headers=auth_harness.bearer(token),
        json={"filename": "scripted.md"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["key"] == f"{connector['storage_prefix']}scripted.md"
    assert body["expires_in"] == 900
    assert body["url"].startswith("http")


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


async def test_a_document_row_carries_its_error_inline(
    auth_harness: AuthHarness, token: str
) -> None:
    """SPEC §13.1 asks for the extraction error in the table. A detail endpoint per failed
    row would mean the table cannot show what is wrong until somebody clicks."""
    connector = await created(auth_harness, token)
    await upload(
        auth_harness.client, auth_harness.bearer(token), connector["id"], ("clip.mov", MOV)
    )
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()

    response = await auth_harness.client.get(
        f"/api/v1/connectors/{connector['id']}/documents", headers=auth_harness.bearer(token)
    )

    [document] = response.json()["items"]
    assert document["status"] == "skipped"
    assert document["error"] == "This video is not a supported format."
    assert document["chunk_count"] == 0


async def test_the_document_list_can_be_filtered_by_status(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)
    await upload(
        auth_harness.client,
        auth_harness.bearer(token),
        connector["id"],
        ("good.md", HANDBOOK),
        ("clip.mov", MOV),
    )
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()

    response = await auth_harness.client.get(
        f"/api/v1/connectors/{connector['id']}/documents?status=indexed",
        headers=auth_harness.bearer(token),
    )

    assert [item["source_name"] for item in response.json()["items"]] == ["good.md"]


async def test_reindexing_a_document_resets_it_to_pending(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)
    upload_response = await upload(
        auth_harness.client, auth_harness.bearer(token), connector["id"], ("bad.json", b"{oh")
    )
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()
    document_id = upload_response.json()["files"][0]["document_id"]

    response = await auth_harness.client.post(
        f"/api/v1/documents/{document_id}/reindex", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["error"] is None


async def test_deleting_a_document_answers_204(auth_harness: AuthHarness, token: str) -> None:
    connector = await created(auth_harness, token)
    upload_response = await upload(
        auth_harness.client, auth_harness.bearer(token), connector["id"], ("a.md", HANDBOOK)
    )
    document_id = upload_response.json()["files"][0]["document_id"]

    response = await auth_harness.client.delete(
        f"/api/v1/documents/{document_id}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 204


async def test_deleting_a_document_that_does_not_exist_is_a_404(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.delete(
        f"/api/v1/documents/{uuid.uuid4()}", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# resync and search
# ---------------------------------------------------------------------------


async def test_resync_reports_the_reconciliation_summary(
    auth_harness: AuthHarness, token: str
) -> None:
    """SPEC §9.1's five counts, which is why this endpoint is synchronous — a job cannot
    return a summary."""
    connector = await created(auth_harness, token)

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/resync", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    assert response.json() == {
        "added": 0,
        "updated": 0,
        "deleted": 0,
        "unchanged": 0,
        "skipped": 0,
    }


async def test_search_returns_scored_chunks_with_their_source(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)
    await upload(
        auth_harness.client, auth_harness.bearer(token), connector["id"], ("handbook.md", HANDBOOK)
    )
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/search",
        headers=auth_harness.bearer(token),
        json={"query": "widgets"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["embedding_model"]
    [hit] = body["hits"]
    assert hit["source_name"] == "handbook.md"
    assert hit["score"] > 0
    assert "widgets" in hit["text"]
    assert hit["chunk_index"] == 0


async def test_an_empty_search_query_is_a_422(auth_harness: AuthHarness, token: str) -> None:
    connector = await created(auth_harness, token)

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/search",
        headers=auth_harness.bearer(token),
        json={"query": ""},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# the chunk inspector (task 11)
# ---------------------------------------------------------------------------


async def test_the_chunk_inspector_lists_what_a_document_became(
    auth_harness: AuthHarness, token: str
) -> None:
    """The fastest way to see whether extraction produced sensible text. A document that
    reports ``indexed`` with a plausible chunk count and answers badly looks identical to
    a healthy one everywhere else on the screen."""
    connector = await created(auth_harness, token)
    await upload(
        auth_harness.client, auth_harness.bearer(token), connector["id"], ("handbook.md", HANDBOOK)
    )
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()
    listed = await auth_harness.client.get(
        f"/api/v1/connectors/{connector['id']}/documents", headers=auth_harness.bearer(token)
    )
    [document] = listed.json()["items"]

    response = await auth_harness.client.get(
        f"/api/v1/documents/{document['id']}/chunks", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["chunk_count"] == document["chunk_count"]
    [chunk] = body["chunks"]
    assert chunk["chunk_index"] == 0
    assert chunk["page_or_section"] == "Handbook"
    assert "widgets" in chunk["text"]
    assert chunk["token_count"] > 0


async def test_the_chunk_inspector_is_a_404_for_a_document_that_does_not_exist(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.get(
        f"/api/v1/documents/{uuid.uuid4()}/chunks", headers=auth_harness.bearer(token)
    )

    assert response.status_code == 404


async def test_a_skipped_document_carries_a_reason_code_beside_its_sentence(
    auth_harness: AuthHarness, token: str
) -> None:
    """The sentence is for a person and gets rewritten as the wording improves; the code
    is what the UI branches on to turn a refusal into an explained state."""
    connector = await created(auth_harness, token)
    await upload(
        auth_harness.client, auth_harness.bearer(token), connector["id"], ("clip.mov", MOV)
    )
    fixture = auth_harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()

    response = await auth_harness.client.get(
        f"/api/v1/connectors/{connector['id']}/documents", headers=auth_harness.bearer(token)
    )

    [document] = response.json()["items"]
    assert document["reason"] == "unsupported_format"
    assert document["page_count"] is None


# ---------------------------------------------------------------------------
# chunking: overrides, compare, and a narrowed reindex (task 20)
# ---------------------------------------------------------------------------


async def indexed(
    harness: AuthHarness, token: str, connector: dict[str, Any], *files: tuple[str, bytes]
) -> list[dict[str, Any]]:
    """Upload, drain the queue, and hand back the document rows."""
    await upload(harness.client, harness.bearer(token), connector["id"], *files)
    fixture = harness.auth.connectors
    assert fixture is not None
    await fixture.run_jobs()
    listed = await harness.client.get(
        f"/api/v1/connectors/{connector['id']}/documents", headers=harness.bearer(token)
    )
    rows: list[dict[str, Any]] = listed.json()["items"]
    return rows


async def test_a_connector_reports_what_each_format_resolves_to(
    auth_harness: AuthHarness, token: str
) -> None:
    """Sent rather than left to the client to recompute. The resolution rule lives in one
    place, and a screen that derived it independently would eventually show a
    configuration the pipeline does not use."""
    connector = await created(
        auth_harness,
        token,
        chunking={"strategy": "recursive", "overrides": {"code": {"strategy": "code"}}},
    )

    assert connector["effective_chunking"]["code"]["strategy"] == "code"
    assert connector["effective_chunking"]["pdf"]["strategy"] == "recursive"
    assert connector["effective_chunking"]["code"]["overrides"] == {}


async def test_an_override_for_an_unknown_format_is_a_422(
    auth_harness: AuthHarness, token: str
) -> None:
    """Refused rather than ignored: an override under a misspelled key is a setting that is
    stored, displayed, and applied to nothing."""
    response = await auth_harness.client.post(
        "/api/v1/connectors",
        headers=auth_harness.bearer(token),
        json={"name": "Typo", "chunking": {"overrides": {"pdfs": {"chunk_size": 500}}}},
    )

    assert response.status_code == 422
    assert "not a format" in response.text


async def test_an_unknown_setting_inside_an_override_is_a_422(
    auth_harness: AuthHarness, token: str
) -> None:
    response = await auth_harness.client.post(
        "/api/v1/connectors",
        headers=auth_harness.bearer(token),
        json={"name": "Typo two", "chunking": {"overrides": {"pdf": {"chunk_sizes": 500}}}},
    )

    assert response.status_code == 422


async def test_a_patch_names_the_formats_it_invalidated(
    auth_harness: AuthHarness, token: str
) -> None:
    """So the prompt can say "reindex the code files" instead of "reindex everything" when
    only an override moved. Since task 104 the answer comes from the rows: the code file
    is marked stale, the Markdown one is not, and a GET a moment later says the same."""
    connector = await created(auth_harness, token)
    await indexed(
        auth_harness,
        token,
        connector,
        ("handbook.md", HANDBOOK),
        ("util.py", b"def alpha(value):\n    return value + 1\n"),
    )

    response = await auth_harness.client.patch(
        f"/api/v1/connectors/{connector['id']}",
        headers=auth_harness.bearer(token),
        json={"chunking": {"overrides": {"code": {"strategy": "code"}}}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reindex_required"] is True
    assert body["reindex_formats"] == ["code"]
    assert body["stale_documents"] == 1
    again = await auth_harness.client.get(
        f"/api/v1/connectors/{connector['id']}", headers=auth_harness.bearer(token)
    )
    assert again.json()["reindex_required"] is True
    assert again.json()["reindex_formats"] == ["code"]
    listed = await auth_harness.client.get("/api/v1/connectors", headers=auth_harness.bearer(token))
    assert {row["id"]: row["stale_documents"] for row in listed.json()["items"]}[
        connector["id"]
    ] == 1


async def test_a_reindex_can_be_narrowed_to_the_formats_that_changed(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)
    await indexed(auth_harness, token, connector, ("handbook.md", HANDBOOK))

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/reindex",
        headers=auth_harness.bearer(token),
        json={"formats": ["code"]},
    )

    assert response.status_code == 200
    assert response.json()["documents"] == 0, "the Markdown file is not a code file"


async def test_a_reindex_with_no_body_still_covers_everything(
    auth_harness: AuthHarness, token: str
) -> None:
    """The old shape, which the UI sends when the connector's own settings moved."""
    connector = await created(auth_harness, token)
    await indexed(auth_harness, token, connector, ("handbook.md", HANDBOOK))

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/reindex", headers=auth_harness.bearer(token)
    )

    assert response.json()["documents"] == 1


async def test_a_reindex_naming_an_unknown_format_is_a_422(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/reindex",
        headers=auth_harness.bearer(token),
        json={"formats": ["pdfs"]},
    )

    assert response.status_code == 422


async def test_compare_returns_a_column_per_candidate_and_writes_nothing(
    auth_harness: AuthHarness, token: str
) -> None:
    """The demoable half of task 20. Without it this ships three more words in a dropdown
    and every user picks by name."""
    connector = await created(auth_harness, token)
    [document] = await indexed(auth_harness, token, connector, ("handbook.md", HANDBOOK))

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/chunking/preview",
        headers=auth_harness.bearer(token),
        json={
            "document_id": document["id"],
            "candidates": [{"label": "tiny", "chunk_size": 50, "overlap": 0}],
            "query": "who pays for widgets",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [candidate["label"] for candidate in body["candidates"]] == ["current", "tiny"]
    assert body["format_kind"] == "markdown"
    for candidate in body["candidates"]:
        assert candidate["distribution"]["chunks"] == candidate["total_chunks"]
        assert candidate["embedded_texts"] > 0
        assert candidate["best"] is not None

    after = await auth_harness.client.get(
        f"/api/v1/documents/{document['id']}/chunks", headers=auth_harness.bearer(token)
    )
    assert after.json()["chunk_count"] == document["chunk_count"]


async def test_compare_refuses_more_candidates_than_it_will_render(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)
    [document] = await indexed(auth_harness, token, connector, ("handbook.md", HANDBOOK))

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/chunking/preview",
        headers=auth_harness.bearer(token),
        json={"document_id": document["id"], "candidates": [{"chunk_size": 60}] * 9},
    )

    assert response.status_code == 422


async def test_compare_is_a_404_for_a_document_that_does_not_exist(
    auth_harness: AuthHarness, token: str
) -> None:
    connector = await created(auth_harness, token)

    response = await auth_harness.client.post(
        f"/api/v1/connectors/{connector['id']}/chunking/preview",
        headers=auth_harness.bearer(token),
        json={"document_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


async def test_every_route_needs_a_token(auth_harness: AuthHarness) -> None:
    """The authenticated-by-default router, doing its job."""
    for method, path in (
        ("GET", "/api/v1/connectors"),
        ("POST", "/api/v1/connectors"),
        ("GET", f"/api/v1/connectors/{uuid.uuid4()}"),
        ("POST", f"/api/v1/connectors/{uuid.uuid4()}/resync"),
        ("DELETE", f"/api/v1/documents/{uuid.uuid4()}"),
    ):
        response = await auth_harness.client.request(method, path)
        assert response.status_code == 401, f"{method} {path}"


async def test_a_viewer_may_read_but_not_write(directory: Any) -> None:
    """``org_viewer`` has ``org:read`` and not ``resources:write``, and connectors are a
    resource like any other."""
    world = directory.world

    listed = await directory.as_user(world.acme_viewer, "GET", "/api/v1/connectors")
    assert listed.status_code == 200

    created_response = await directory.as_user(
        world.acme_viewer, "POST", "/api/v1/connectors", json_body={"name": "Nope"}
    )
    assert created_response.status_code == 403
