"""Templates through the real route (task 105).

The same harness as the citation tests: authentication, retrieval over the in-process
index, routing, the adapter, a scriptable provider. What is asserted is the prompt the
provider received, what the client received around the answer, and what the log row says
about which wording produced it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from app.schemas.gateway_config import MemoryConfig, TemplateConfig
from app.services.templates import DEFAULT_TEMPLATES, Templates
from tests.conftest import ProxyHarness
from tests.support import Behaviour, anthropic_events, chunk, completion
from tests.test_proxy_citations import ASK, CONNECTOR, FACTS, content_of, frames_of, row


def configure(proxy: ProxyHarness, *, citations: str = "off", **templates: str) -> None:
    memory = MemoryConfig.model_validate(
        {"connector_ids": [str(CONNECTOR)], "doc_min_score": 0.0, "citations": citations}
    )
    proxy.resolver.gateway = replace(
        proxy.gateway,
        memory=memory,
        templates=Templates.of(TemplateConfig.model_validate(templates)),
    )


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


def system_sent(proxy: ProxyHarness) -> str:
    return str(proxy.upstream.last_request.body["messages"][0]["content"])


# ---------------------------------------------------------------------------
# the request side
# ---------------------------------------------------------------------------


async def test_the_reference_block_is_worded_by_the_gateway(proxy: ProxyHarness) -> None:
    await seed(proxy)
    configure(
        proxy,
        reference_heading="## Referenzmaterial",
        reference_instruction="Zitiere die Auszüge, wenn sie passen.",
        excerpt="[{handle}] {source_name}{section}\n{text}",
    )

    response = await ask(proxy, "Neunzehn Monate [1].")

    assert response.status_code == 200
    system = system_sent(proxy)
    assert system.startswith("## Referenzmaterial\nZitiere die Auszüge, wenn sie passen.\n\n[1] ")
    assert "source:" not in system
    assert "## Reference material" not in system
    # The stored transcript is the prompt the provider received, German block included.
    logged = await row(proxy)
    assert logged.template_fingerprint == proxy.gateway.template_fingerprint
    assert logged.template_fingerprint != DEFAULT_TEMPLATES.fingerprint


async def test_citations_resolve_under_any_excerpt_template_that_keeps_the_handle(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, citations="metadata", excerpt="{source_name} → [{handle}]\n{text}")

    response = await ask(proxy, "as [2] and [1] say")

    message = response.json()["choices"][0]["message"]
    assert [c["handle"] for c in message["citations"]] == [2, 1]
    assert message["citations_unresolved"] == []


async def test_the_default_templates_write_the_default_fingerprint_and_nothing_else(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy)

    response = await ask(proxy, "Nineteen months.")

    assert response.json()["choices"][0]["message"]["content"] == "Nineteen months."
    assert system_sent(proxy).startswith("## Reference material\nThe following excerpts")
    logged = await row(proxy)
    assert logged.template_fingerprint == DEFAULT_TEMPLATES.fingerprint


# ---------------------------------------------------------------------------
# the response side
# ---------------------------------------------------------------------------


async def test_prefix_and_suffix_wrap_a_non_streamed_answer_around_the_footer(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(
        proxy,
        citations="footer",
        sources_heading="Quellen:",
        source_line="{handle}. {source_name}",
        answer_prefix="[{gateway}] ",
        answer_suffix="\n\n_{cited_count} of {injected_count} documents cited via {model}._",
    )

    response = await ask(proxy, "Nineteen months [1].")

    content = response.json()["choices"][0]["message"]["content"]
    assert content == (
        f"[{proxy.gateway.name}] Nineteen months [1].\n\nQuellen:\n1. warranty.md"
        f"\n\n_1 of 2 documents cited via {proxy.target.name}._"
    )
    # The provider's usage is forwarded as received; the wrapping is not counted.
    assert response.json()["usage"] == completion("Nineteen months [1].")["usage"]


async def test_streamed_prefix_is_the_first_content_frame_and_suffix_precedes_done(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(
        proxy,
        citations="footer",
        answer_prefix="> ",
        answer_suffix="\n\n_Generated from internal documents._",
    )
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("Nineteen "), chunk("months [1].")])

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    deltas = [json.loads(frame)["choices"][0]["delta"] for frame in frames if frame != "[DONE]"]
    assert deltas[0] == {"content": "> "}
    assert frames[1] == chunk("Nineteen ")
    assert frames[2] == chunk("months [1].")
    assert deltas[3]["content"].startswith("\n\nSources:\n[1] ")
    assert deltas[4] == {"content": "\n\n_Generated from internal documents._"}
    assert frames[-1] == "[DONE]"
    assert len(frames) == 6
    # The prefix frame carries the provider's own id, so a client grouping by id keeps it.
    assert json.loads(frames[0])["id"] == json.loads(frames[1])["id"]


async def test_wrapping_applies_with_nothing_injected(proxy: ProxyHarness) -> None:
    """The prefix and suffix are the gateway's text, not the documents': a gateway with
    no memory still wraps, streamed and not."""
    proxy.resolver.gateway = replace(
        proxy.gateway, templates=Templates(answer_prefix="A: ", answer_suffix=" ({cited_count})")
    )

    response = await ask(proxy, "Hello.")
    assert response.json()["choices"][0]["message"]["content"] == "A: Hello. (0)"

    proxy.logs.rows.clear()
    response = await ask(proxy, "Hello.", stream=True)
    assert content_of(frames_of(response.text)) == "A: Hello. (0)"


async def test_empty_prefix_and_suffix_add_no_frame(proxy: ProxyHarness) -> None:
    """With both empty the stream is frame-for-frame what task 100 produced."""
    await seed(proxy)
    configure(proxy, citations="footer")
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("Nineteen "), chunk("months [1].")])

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    assert frames[:2] == [chunk("Nineteen "), chunk("months [1].")]
    assert len(frames) == 4
    assert frames[-1] == "[DONE]"


async def test_streamed_metadata_puts_the_suffix_before_the_citations_chunk(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy)
    configure(proxy, citations="metadata", answer_suffix=" [end]")
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("as [2] says")])

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    deltas = [json.loads(frame)["choices"][0]["delta"] for frame in frames if frame != "[DONE]"]
    assert deltas[1] == {"content": " [end]"}
    assert [c["handle"] for c in deltas[2]["citations"]] == [2]
    assert frames[-1] == "[DONE]"


# ---------------------------------------------------------------------------
# the fingerprint
# ---------------------------------------------------------------------------


async def test_two_gateways_with_different_templates_write_two_fingerprints(
    proxy: ProxyHarness,
) -> None:
    configure(proxy, reference_heading="## A")
    await ask(proxy, "one")
    first = proxy.gateway.template_fingerprint

    configure(proxy, reference_heading="## B")
    await ask(proxy, "two")
    second = proxy.gateway.template_fingerprint

    await proxy.logs.flush()
    assert first != second
    assert [entry.template_fingerprint for entry in proxy.logs.rows] == [first, second]


async def test_editing_a_template_changes_the_next_row_and_not_the_last(
    proxy: ProxyHarness,
) -> None:
    configure(proxy)
    await ask(proxy, "one")
    await proxy.logs.flush()
    before = proxy.logs.rows[0].template_fingerprint

    configure(proxy, answer_suffix=" ✓")
    await ask(proxy, "two")
    await proxy.logs.flush()

    assert proxy.logs.rows[0].template_fingerprint == before == DEFAULT_TEMPLATES.fingerprint
    assert proxy.logs.rows[1].template_fingerprint != before


# ---------------------------------------------------------------------------
# the Anthropic dialect
# ---------------------------------------------------------------------------


@pytest.fixture
async def claude(live_proxy: ProxyHarness) -> ProxyHarness:
    live_proxy.retarget(dialect="anthropic", auth_type="api_key_header")
    return live_proxy


async def test_anthropic_streaming_wraps_the_answer(claude: ProxyHarness) -> None:
    proxy = claude
    await seed(proxy)
    configure(proxy, citations="footer", answer_prefix="» ", answer_suffix=" «")
    proxy.upstream.behaviour = Behaviour(
        chunks=anthropic_events("Nineteen ", "months [1]."), send_done=False
    )

    response = await proxy.client.post(
        proxy.url(), json={**ASK, "stream": True}, headers=proxy.headers()
    )
    frames = frames_of(response.text)

    text = content_of(frames)
    assert text.startswith("» Nineteen months [1].\n\nSources:\n[1] ")
    assert text.endswith(" «")
    assert frames[-1] == "[DONE]"
