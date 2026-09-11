"""Citations through the real route (task 100).

Everything here goes through the app — authentication, retrieval over the in-process
index, routing, the adapter, a scriptable provider — and the provider is scripted to
*cite*: it answers ``as [1] says``, which is what a model does with the prompt §7 builds.
What is asserted is what the client received and what the log row says.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any

import pytest

from app.schemas.gateway_config import MemoryConfig
from app.services.citations import FOOTER_HEADING
from tests.conftest import ProxyHarness
from tests.support import Behaviour, anthropic_events, anthropic_message, chunk, completion

CONNECTOR = uuid.UUID(int=99)

ASK: dict[str, Any] = {
    "model": "demo",
    "messages": [{"role": "user", "content": "warranty on the Zynthorp stabiliser?"}],
}
#: Two chunks in two documents — neighbouring chunks of one document are deduplicated by
#: retrieval — both overlapping the question so both clear the score floor under the
#: lexical test embedder. The prompt numbers them [1] and [2], best score first.
FACTS = (
    ("Warranty on the Zynthorp stabiliser: 19 months.", "warranty.md"),
    (
        "Warranty on the Zynthorp stabiliser QX-4471 is honoured only at the Utrecht depot.",
        "servicing.md",
    ),
)


def configure(proxy: ProxyHarness, citations: str, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "connector_ids": [str(CONNECTOR)],
        "doc_min_score": 0.0,
        "citations": citations,
    }
    values.update(overrides)
    proxy.resolver.gateway = replace(proxy.gateway, memory=MemoryConfig.model_validate(values))


async def seed(proxy: ProxyHarness) -> None:
    for text, source in FACTS:
        await proxy.memory.index(proxy.gateway.organization_id, CONNECTOR, text, source=source)


async def ask(proxy: ProxyHarness, answer: str, *, stream: bool = False) -> Any:
    if stream:
        proxy.upstream.behaviour = Behaviour(chunks=[chunk(answer)])
    else:
        proxy.upstream.behaviour.body = completion(answer)
    body = {**ASK, "stream": True} if stream else ASK
    return await proxy.client.post(proxy.url(), json=body, headers=proxy.headers())


def frames_of(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


def content_of(frames: list[str]) -> str:
    return "".join(
        choice["delta"].get("content") or ""
        for frame in frames
        if frame != "[DONE]"
        for choice in json.loads(frame)["choices"]
    )


async def row(proxy: ProxyHarness) -> Any:
    await proxy.logs.flush()
    assert len(proxy.logs.rows) == 1
    return proxy.logs.rows[0]


def injected_ids(log_row: Any) -> list[str]:
    return [entry["id"] for entry in log_row.retrieved_chunk_ids if entry.get("injected")]


# ---------------------------------------------------------------------------
# the demo: metadata
# ---------------------------------------------------------------------------


async def test_metadata_resolves_a_handle_to_the_document_the_prompt_named(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, "metadata")

    response = await ask(proxy, "Nineteen months, as [1] says.")

    assert response.status_code == 200
    message = response.json()["choices"][0]["message"]
    assert message["content"] == "Nineteen months, as [1] says."
    [citation] = message["citations"]
    assert citation["handle"] == 1
    assert citation["document_name"] == "warranty.md"
    assert citation["connector_id"] == str(CONNECTOR)
    assert citation["url"].startswith("http://localhost:8000/connectors/")
    assert message["citations_unresolved"] == []
    # The chunk it names is the one the prompt numbered [1] — asserted against the log's
    # own record of what was injected, in order.
    logged = await row(proxy)
    assert citation["chunk_id"] == injected_ids(logged)[0]
    assert logged.cited_chunk_ids == [citation["chunk_id"]]


async def test_metadata_reports_a_hallucinated_handle_and_leaves_the_text_alone(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, "metadata")

    response = await ask(proxy, "See [1] and [7].")

    message = response.json()["choices"][0]["message"]
    assert message["content"] == "See [1] and [7]."
    assert [c["handle"] for c in message["citations"]] == [1]
    assert message["citations_unresolved"] == [7]
    assert (await row(proxy)).citations_unresolved == 1


async def test_an_answer_that_cites_nothing_has_an_empty_array(proxy: ProxyHarness) -> None:
    await seed(proxy)
    configure(proxy, "metadata")

    response = await ask(proxy, "The documents do not say.")

    message = response.json()["choices"][0]["message"]
    assert message["citations"] == []
    logged = await row(proxy)
    assert injected_ids(logged) and logged.cited_chunk_ids == []


# ---------------------------------------------------------------------------
# footer
# ---------------------------------------------------------------------------


async def test_footer_appends_sources_and_strips_a_hallucinated_handle(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, "footer")

    response = await ask(proxy, "Nineteen months [1], serviced in Utrecht [2], see [9].")

    content = response.json()["choices"][0]["message"]["content"]
    body, _, sources = content.partition(f"\n\n{FOOTER_HEADING}\n")
    assert body == "Nineteen months [1], serviced in Utrecht [2], see."
    lines = sources.splitlines()
    assert lines[0].startswith("[1] [warranty.md](http://localhost:8000/connectors/")
    assert lines[1].startswith("[2] [servicing.md](")
    assert "citations" not in response.json()["choices"][0]["message"]
    # The provider's usage is forwarded as received; the footer is not billed to it.
    assert response.json()["usage"] == completion("x")["usage"]


async def test_footer_appends_nothing_when_nothing_was_cited(proxy: ProxyHarness) -> None:
    await seed(proxy)
    configure(proxy, "footer")

    response = await ask(proxy, "No idea.")

    assert response.json()["choices"][0]["message"]["content"] == "No idea."


# ---------------------------------------------------------------------------
# off
# ---------------------------------------------------------------------------


async def test_off_returns_the_upstream_body_untouched_and_still_records(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, "off")

    response = await ask(proxy, "As [1] says, and [7].")

    assert response.json() == completion("As [1] says, and [7].")
    logged = await row(proxy)
    assert logged.cited_chunk_ids == [injected_ids(logged)[0]]
    assert logged.citations_unresolved == 1


async def test_the_default_mode_is_off(proxy: ProxyHarness) -> None:
    assert MemoryConfig().citations == "off"


# ---------------------------------------------------------------------------
# numbering parity
# ---------------------------------------------------------------------------


async def test_a_chunk_dropped_by_the_budget_never_resolves(proxy: ProxyHarness) -> None:
    """The handles are the assembler's. With room for one chunk, ``[2]`` names nothing —
    even though retrieval found two — because the prompt never showed a ``[2]``."""
    await seed(proxy)
    configure(proxy, "metadata", doc_max_tokens=75)

    response = await ask(proxy, "See [1] and [2].")

    message = response.json()["choices"][0]["message"]
    logged = await row(proxy)
    assert len(injected_ids(logged)) == 1, "the budget should have dropped one chunk"
    assert [c["handle"] for c in message["citations"]] == [1]
    assert message["citations_unresolved"] == [2]


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_streamed_footer_is_one_extra_delta_before_done(proxy: ProxyHarness) -> None:
    await seed(proxy)
    configure(proxy, "footer")
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("Nineteen "), chunk("months [1].")])

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    assert frames[-1] == "[DONE]"
    # The provider's frames, byte for byte, then exactly one delta of the gateway's own.
    assert frames[0] == chunk("Nineteen ")
    assert frames[1] == chunk("months [1].")
    footer = json.loads(frames[2])["choices"][0]["delta"]["content"]
    assert footer.startswith(f"\n\n{FOOTER_HEADING}\n[1] [warranty.md](")
    assert len(frames) == 4


async def test_a_handle_split_across_frames_resolves(proxy: ProxyHarness) -> None:
    await seed(proxy)
    configure(proxy, "footer")
    proxy.upstream.behaviour = Behaviour(
        chunks=[chunk("see ["), chunk("1] and ["), chunk("9"), chunk("]. done")]
    )

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    body, _, sources = content_of(frames).partition(f"\n\n{FOOTER_HEADING}\n")
    assert body == "see [1] and. done"
    assert sources.startswith("[1] ")
    logged = await row(proxy)
    assert logged.cited_chunk_ids == [injected_ids(logged)[0]]
    assert logged.citations_unresolved == 1


async def test_streamed_metadata_arrives_on_a_final_chunk_with_no_content(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, "metadata")
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("as ["), chunk("2] says")])

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    assert frames[:2] == [chunk("as ["), chunk("2] says")]
    delta = json.loads(frames[2])["choices"][0]["delta"]
    assert "content" not in delta
    assert [c["handle"] for c in delta["citations"]] == [2]
    assert delta["citations_unresolved"] == []
    assert frames[3] == "[DONE]"


async def test_a_streamed_off_gateway_relays_frames_untouched_and_records(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, "off")
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("as [2] says [8]")])

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )

    assert frames_of(response.text) == [chunk("as [2] says [8]"), "[DONE]"]
    logged = await row(proxy)
    assert logged.cited_chunk_ids == [injected_ids(logged)[1]]
    assert logged.citations_unresolved == 1


async def test_the_stored_transcript_is_what_the_client_received(proxy: ProxyHarness) -> None:
    """Footer included: the transcript is the answer as delivered, and the drawer can say
    which part the gateway added because the mode is on the gateway."""
    await seed(proxy)
    configure(proxy, "footer")

    await ask(proxy, "Nineteen months [1].", stream=True)

    logged = await row(proxy)
    transcript = proxy.logs.transcript(logged.id)
    assert transcript is not None and transcript.response_body is not None
    assert transcript.response_body.startswith("Nineteen months [1].\n\nSources:\n[1] ")


# ---------------------------------------------------------------------------
# no documents, no stage
# ---------------------------------------------------------------------------


async def test_a_gateway_without_memory_is_untouched_in_every_mode(
    proxy: ProxyHarness,
) -> None:
    proxy.resolver.gateway = replace(
        proxy.gateway, memory=MemoryConfig.model_validate({"citations": "footer"})
    )

    response = await ask(proxy, "Bare answer [1].")

    assert response.json() == completion("Bare answer [1].")
    logged = await row(proxy)
    assert logged.cited_chunk_ids == [] and logged.citations_unresolved == 0


# ---------------------------------------------------------------------------
# the other dialect
# ---------------------------------------------------------------------------


@pytest.fixture
async def claude(live_proxy: ProxyHarness) -> ProxyHarness:
    live_proxy.retarget(dialect="anthropic", auth_type="api_key_header")
    return live_proxy


async def test_anthropic_metadata_non_streaming(claude: ProxyHarness) -> None:
    await seed(claude)
    configure(claude, "metadata")
    claude.upstream.behaviour.body = anthropic_message("Nineteen months [1].")

    response = await claude.client.post(claude.url(), json=ASK, headers=claude.headers())

    message = response.json()["choices"][0]["message"]
    assert [c["handle"] for c in message["citations"]] == [1]
    assert message["citations"][0]["document_name"] == "warranty.md"


async def test_anthropic_footer_streaming(claude: ProxyHarness) -> None:
    await seed(claude)
    configure(claude, "footer")
    claude.upstream.behaviour = Behaviour(
        chunks=anthropic_events("Nineteen months [", "1], see [7]."), send_done=False
    )

    response = await claude.client.post(
        claude.url(), json={**ASK, "stream": True}, headers=claude.headers()
    )
    frames = frames_of(response.text)

    assert frames[-1] == "[DONE]"
    body, _, sources = content_of(frames).partition(f"\n\n{FOOTER_HEADING}\n")
    assert body == "Nineteen months [1], see."
    assert sources.startswith("[1] [warranty.md](")
    logged = await row(claude)
    assert logged.cited_chunk_ids == [injected_ids(logged)[0]]
    assert logged.citations_unresolved == 1


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


async def test_the_three_counters_move(proxy: ProxyHarness) -> None:
    await seed(proxy)
    configure(proxy, "off")
    slug = proxy.gateway.slug

    await ask(proxy, "See [1] and [2] and [9].")
    await row(proxy)
    proxy.logs.database.request_logs.clear()
    await ask(proxy, "Nothing cited.")
    await proxy.logs.flush()

    sample = proxy.logs.registry.get_sample_value
    assert sample("citations_resolved_total", {"gateway": slug}) == 2
    assert sample("citations_unresolved_total", {"gateway": slug}) == 1
    assert sample("requests_uncited_total", {"gateway": slug}) == 1
