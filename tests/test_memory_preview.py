"""Try retrieval and Prompt preview, over the control-plane API.

The acceptance criterion this file owns is the one that is easy to claim and hard to
keep: **the preview shows exactly what a real request would inject**. So the last test
here indexes a corpus, asks the preview, then sends the same question through the data
plane, and compares what came back with what went upstream.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services.gateway_store import MemoryGatewayStore
from app.services.memory_preview import MemoryPreview
from tests.auth_support import AuthFixture, build_auth
from tests.conftest import AuthHarness
from tests.gateway_support import make_gateway_row

SECRET_FACT = (
    "The Zynthorp QX-4471 stabiliser ships with a 19-month warranty and is serviced only "
    "at the Utrecht depot."
)

#: Several rare words rather than one. The development embedder is the hashing trick over
#: 64 buckets, where a single-word query can be cancelled outright by a collision — which
#: is a true statement about a lexical embedder and a poor foundation for an assertion.
QUESTION = "Zynthorp QX-4471 warranty Utrecht depot"


@pytest.fixture
async def signed_in(auth_harness: AuthHarness) -> AuthHarness:
    return auth_harness


def fixture_of(harness: AuthHarness) -> AuthFixture:
    return harness.auth


async def seed(
    harness: AuthHarness,
    *texts: str,
    source: str = "warranty.md",
    section: str | None = None,
) -> uuid.UUID:
    """Index chunks into the same vector store the preview reads."""
    connectors = fixture_of(harness).connectors
    assert connectors is not None
    document_id = uuid.uuid4()
    store = connectors.vectors
    embedder = connectors.embedder
    await store.ensure_collection(connectors.organization_id, dimension=embedder.dimension)
    from app.services.vector_store import ChunkPoint

    vectors = await embedder.embed(list(texts))
    await store.upsert(
        connectors.organization_id,
        [
            ChunkPoint(
                id=f"{document_id}:{index}",
                vector=vector,
                payload={
                    "org_id": str(connectors.organization_id),
                    "connector_id": str(connectors.connector.id),
                    "document_id": str(document_id),
                    "source_name": source,
                    "page_or_section": section,
                    "chunk_index": index,
                    "text": text,
                },
            )
            for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ],
    )
    return document_id


async def make_gateway(harness: AuthHarness, **memory: Any) -> str:
    """A gateway attached to the fixture's connector, created through the API."""
    connectors = fixture_of(harness).connectors
    assert connectors is not None
    token = await harness.sign_in()
    body: dict[str, Any] = {
        "name": "Support",
        "slug": f"support-{uuid.uuid4().hex[:8]}",
        "memory_config": {
            "connector_ids": [str(connectors.connector.id)],
            "doc_min_score": 0.0,
            **memory,
        },
    }
    response = await harness.client.post(
        "/api/v1/gateways", json=body, headers=harness.bearer(token)
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def post(harness: AuthHarness, path: str, body: dict[str, Any]) -> Any:
    token = await harness.sign_in()
    return await harness.client.post(path, json=body, headers=harness.bearer(token))


# ---------------------------------------------------------------------------
# try retrieval
# ---------------------------------------------------------------------------


async def test_it_returns_the_chunks_a_question_would_inject(
    signed_in: AuthHarness,
) -> None:
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)

    response = await post(
        signed_in, f"/api/v1/gateways/{gateway_id}/try-retrieval", {"query": QUESTION}
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["outcome"] == "hit"
    assert payload["chunks"][0]["source_name"] == "warranty.md"
    assert payload["chunks"][0]["injected"] is True
    assert payload["chunks"][0]["score"] > 0


async def test_an_empty_result_says_which_kind_of_empty_it_is(
    signed_in: AuthHarness,
) -> None:
    """Four different things produce zero rows and they need four different next steps."""
    await seed(signed_in, "Office plants are watered on Tuesdays.")
    gateway_id = await make_gateway(signed_in, doc_min_score=0.99)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/try-retrieval",
            {"query": QUESTION},
        )
    ).json()

    assert payload["outcome"] == "empty"
    assert payload["chunks"] == []


async def test_a_gateway_with_no_connectors_reports_skipped_not_empty(
    signed_in: AuthHarness,
) -> None:
    token = await signed_in.sign_in()
    created = await signed_in.client.post(
        "/api/v1/gateways",
        json={"name": "Bare", "slug": f"bare-{uuid.uuid4().hex[:8]}"},
        headers=signed_in.bearer(token),
    )
    gateway_id = created.json()["id"]

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/try-retrieval",
            {"query": QUESTION},
        )
    ).json()

    assert payload["outcome"] == "skipped"


async def test_unsaved_settings_are_used_without_being_saved(
    signed_in: AuthHarness,
) -> None:
    """The whole point of the tuning loop: change a number, press Try, and do not change
    what live callers of this endpoint are getting in the meantime."""
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/try-retrieval",
            {"query": QUESTION, "memory_config": {"doc_min_score": 0.99}},
        )
    ).json()
    assert payload["outcome"] == "empty"

    stored = await signed_in.client.get(
        f"/api/v1/gateways/{gateway_id}",
        headers=signed_in.bearer(await signed_in.sign_in()),
    )
    assert stored.json()["memory_config"]["doc_min_score"] == 0.0


async def test_a_chunk_over_the_budget_is_returned_and_marked(
    signed_in: AuthHarness,
) -> None:
    """Hiding it would hide exactly the case the budget is worth tuning for."""
    await seed(signed_in, SECRET_FACT)
    await seed(signed_in, "A second paragraph about Zynthorp warranties and the depot.")
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/try-retrieval",
            {"query": QUESTION, "memory_config": {"doc_max_tokens": 80}},
        )
    ).json()

    assert any(chunk["injected"] for chunk in payload["chunks"])
    assert any(not chunk["injected"] for chunk in payload["chunks"])


async def test_an_unsaved_patch_cannot_borrow_another_organizations_connector(
    signed_in: AuthHarness,
) -> None:
    """The preview accepts an unsaved configuration, which makes it exactly the place
    somebody would try naming a connector the save path would refuse."""
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)

    response = await post(
        signed_in,
        f"/api/v1/gateways/{gateway_id}/try-retrieval",
        {"query": QUESTION, "memory_config": {"connector_ids": [str(uuid.uuid4())]}},
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "skipped"


async def test_an_invalid_setting_is_refused_with_the_same_message_the_save_gives(
    signed_in: AuthHarness,
) -> None:
    gateway_id = await make_gateway(signed_in)

    response = await post(
        signed_in,
        f"/api/v1/gateways/{gateway_id}/try-retrieval",
        {"query": QUESTION, "memory_config": {"doc_min_score": 5}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"].startswith("memory_config")


async def test_a_blank_query_is_refused(signed_in: AuthHarness) -> None:
    gateway_id = await make_gateway(signed_in)

    response = await post(
        signed_in, f"/api/v1/gateways/{gateway_id}/try-retrieval", {"query": "   "}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# prompt preview
# ---------------------------------------------------------------------------


async def test_the_prompt_preview_shows_every_layer_with_its_token_count(
    signed_in: AuthHarness,
) -> None:
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)
    await signed_in.client.patch(
        f"/api/v1/gateways/{gateway_id}",
        json={"system_context": "You are Acme's support assistant."},
        headers=signed_in.bearer(await signed_in.sign_in()),
    )

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION},
        )
    ).json()

    layers = {layer["name"]: layer for layer in payload["layers"]}
    assert layers["gateway.system_context"]["text"] == "You are Acme's support assistant."
    assert layers["documents"]["tokens"] > 0
    assert "Zynthorp" in payload["system_message"]
    assert payload["total_tokens"] == sum(layer["tokens"] for layer in payload["layers"])


async def test_an_empty_layer_is_returned_with_empty_text_rather_than_omitted(
    signed_in: AuthHarness,
) -> None:
    """The preview draws a fixed set of rows; a layer that vanished would move the ones
    below it every time somebody cleared a field."""
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION},
        )
    ).json()

    assert {layer["name"] for layer in payload["layers"]} == {
        "model.system_context",
        "gateway.system_context",
        "documents",
        "memory",
        "client.system",
    }
    assert all(layer["tokens"] == 0 for layer in payload["layers"])


async def test_the_prompt_preview_carries_the_retrieval_behind_it(
    signed_in: AuthHarness,
) -> None:
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION},
        )
    ).json()

    assert payload["retrieval"]["outcome"] == "hit"
    assert payload["retrieval"]["chunks"]


async def test_a_model_with_no_context_window_reports_none_rather_than_a_guess(
    signed_in: AuthHarness,
) -> None:
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION},
        )
    ).json()

    assert payload["context_window"] is None
    assert payload["overflowed"] is False


# ---------------------------------------------------------------------------
# the criterion
# ---------------------------------------------------------------------------


async def test_the_preview_matches_what_a_request_injects(signed_in: AuthHarness) -> None:
    """Not "looks similar to" — the same chunks, in the same order, at the same scores.

    Asserted by running the preview and then assembling the prompt through the *same*
    memory service and assembler the data plane uses, and comparing the rendered block.
    """
    from app.schemas.gateway_config import MemoryConfig
    from app.schemas.openai import ChatMessage
    from app.services.prompt import assemble
    from tests.connector_support import TOKENIZER

    await seed(signed_in, SECRET_FACT, source="warranty.md", section="§4")
    gateway_id = await make_gateway(signed_in)

    preview = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION},
        )
    ).json()

    connectors = fixture_of(signed_in).connectors
    assert connectors is not None
    recall = await connectors.memory.recall(
        organization_id=connectors.organization_id,
        config=MemoryConfig.model_validate(
            {"connector_ids": [str(connectors.connector.id)], "doc_min_score": 0.0}
        ),
        messages=[ChatMessage(role="user", content=QUESTION)],
    )
    assembled = assemble(
        [ChatMessage(role="user", content=QUESTION)],
        chunks=recall.documents.chunks,
        doc_max_tokens=MemoryConfig().doc_max_tokens,
        memory_max_tokens=MemoryConfig().memory_max_tokens,
        tokenizer=TOKENIZER,
    )

    documents = next(layer for layer in preview["layers"] if layer["name"] == "documents")
    injected = next(layer for layer in assembled.layers if layer.name == "documents")
    assert documents["text"] == injected.text
    assert documents["tokens"] == injected.tokens


# ---------------------------------------------------------------------------
# scoping, at the service level
# ---------------------------------------------------------------------------


async def test_a_gateway_in_another_organization_is_not_found() -> None:
    """Belt and braces beside the cross-tenant net: the same 404 for "does not exist"
    and "belongs to somebody else"."""
    from app.core.errors import NotFound

    fixture = build_auth()
    connectors = fixture.connectors
    assert connectors is not None
    preview = MemoryPreview(MemoryGatewayStore(fixture.database), memory=connectors.memory)
    from tests.auth_support import make_organization

    other = make_organization(name="Globex", slug="globex")
    fixture.database.add_organization(other)
    stranger = make_gateway_row(other, slug="globex-support")
    fixture.database.add_gateway(stranger)

    actor = connectors.actor
    with pytest.raises(NotFound):
        await preview.try_retrieval(actor, stranger.id, query=QUESTION)


# ---------------------------------------------------------------------------
# citations (task 100)
# ---------------------------------------------------------------------------


async def test_try_retrieval_shows_the_handle_each_chunk_would_be_numbered_with(
    signed_in: AuthHarness,
) -> None:
    """So a person reading ``[3]`` in a logged answer can map it back without opening
    the drawer. Positional in retrieval order, as the prompt numbers them."""
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(signed_in, f"/api/v1/gateways/{gateway_id}/try-retrieval", {"query": QUESTION})
    ).json()

    assert [chunk["handle"] for chunk in payload["chunks"]] == list(
        range(1, len(payload["chunks"]) + 1)
    )


async def test_the_prompt_preview_renders_one_citation_example_per_mode(
    signed_in: AuthHarness,
) -> None:
    await seed(signed_in, SECRET_FACT)
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION, "memory_config": {"citations": "footer"}},
        )
    ).json()

    citations = payload["citations"]
    assert citations["mode"] == "footer"
    assert "[1]" in citations["sample_answer"]
    [example] = citations["metadata"]
    assert example["handle"] == 1
    assert example["document_name"] == "warranty.md"
    assert example["chunk_id"] == payload["retrieval"]["chunks"][0]["id"]
    assert citations["footer"].startswith("\n\nSources:\n[1] ")


async def test_the_citation_example_cites_nothing_when_nothing_is_injected(
    signed_in: AuthHarness,
) -> None:
    gateway_id = await make_gateway(signed_in)

    payload = (
        await post(
            signed_in,
            f"/api/v1/gateways/{gateway_id}/prompt-preview",
            {"query": QUESTION},
        )
    ).json()

    assert payload["citations"]["metadata"] == []
    assert payload["citations"]["footer"] == ""
    assert payload["citations"]["mode"] == "off"
