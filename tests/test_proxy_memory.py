"""The product's promise, driven through the real route: the model answers from the
organization's documents.

The corpus contains a **made-up product code** that no model has ever been trained on, so
"the answer contains the code" can only be true if the code reached the provider in this
request. That is the whole design of the grounding test: it is checkable without a real
model, because what is asserted is what the *gateway sent*, not what a model said back.

Everything here goes through the app — authentication, routing, the adapter, a scriptable
provider on a real socket. Only the four sockets are memory implementations.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest

from app.api.proxy.routes import (
    CHUNKS_HEADER,
    MEMORY_HEADER,
    MEMORY_OFF,
    RETRIEVAL_MS_HEADER,
)
from app.schemas.gateway_config import MemoryConfig
from app.services.retrieval import MemoryService, Recall, Retrieval
from tests.conftest import ProxyHarness
from tests.support import completion

CONNECTOR = uuid.UUID(int=99)
OTHER_CONNECTOR = uuid.UUID(int=98)

#: A product code that cannot be in any model's training data. If it comes out of the
#: gateway, it went in through retrieval.
SECRET_FACT = (
    "The Zynthorp QX-4471 stabiliser ships with a 19-month warranty and is serviced only "
    "at the Utrecht depot."
)

ASK = {
    "model": "demo",
    "messages": [{"role": "user", "content": "warranty on the Zynthorp stabiliser?"}],
}


def with_memory(proxy: ProxyHarness, **overrides: Any) -> None:
    """Attach connectors to the gateway, exactly as a save would."""
    values: dict[str, Any] = {"connector_ids": [str(CONNECTOR)], "doc_min_score": 0.0}
    values.update(overrides)
    proxy.resolver.gateway = replace(proxy.gateway, memory=MemoryConfig.model_validate(values))


async def seed(proxy: ProxyHarness, *texts: str, connector: uuid.UUID = CONNECTOR) -> uuid.UUID:
    return await proxy.memory.index(
        proxy.gateway.organization_id, connector, *texts, source="warranty.md"
    )


async def send(proxy: ProxyHarness, **kwargs: Any) -> Any:
    proxy.upstream.behaviour.body = completion("hello")
    headers = {**proxy.headers(), **kwargs.pop("headers", {})}
    return await proxy.client.post(proxy.url(), json=ASK, headers=headers, **kwargs)


def sent_prompt(proxy: ProxyHarness) -> str:
    """The system message the provider actually received."""
    messages = proxy.upstream.last_request.body["messages"]
    return next((m["content"] for m in messages if m["role"] == "system"), "")


async def row(proxy: ProxyHarness) -> Any:
    await proxy.logs.flush()
    assert len(proxy.logs.rows) == 1
    return proxy.logs.rows[0]


# ---------------------------------------------------------------------------
# the A/B that proves the feature
# ---------------------------------------------------------------------------


async def test_a_question_answerable_only_from_a_document_reaches_the_model(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    response = await send(proxy)

    assert response.status_code == 200
    assert "Zynthorp QX-4471" in sent_prompt(proxy)
    assert "19-month warranty" in sent_prompt(proxy)


async def test_the_same_request_with_memory_off_carries_none_of_it(
    proxy: ProxyHarness,
) -> None:
    """The other half of the A/B, and the reason the header exists: measuring what the
    gateway contributes by editing its configuration would change what every other caller
    gets at the same time."""
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    response = await send(proxy, headers={MEMORY_HEADER: MEMORY_OFF})

    assert response.status_code == 200
    assert "Zynthorp" not in sent_prompt(proxy)
    # No retrieval happened at all, so neither header is present.
    assert CHUNKS_HEADER not in response.headers


async def test_memory_off_is_case_insensitive(proxy: ProxyHarness) -> None:
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    await send(proxy, headers={MEMORY_HEADER: " OFF "})

    assert "Zynthorp" not in sent_prompt(proxy)


async def test_any_other_value_of_the_header_leaves_memory_on(proxy: ProxyHarness) -> None:
    """A caller sending `X-Gateway-Memory: on` means "yes please", and a typo must not
    silently disable the feature the endpoint is for."""
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    await send(proxy, headers={MEMORY_HEADER: "on"})

    assert "Zynthorp" in sent_prompt(proxy)


# ---------------------------------------------------------------------------
# no documents
# ---------------------------------------------------------------------------


async def test_nothing_relevant_injects_no_reference_block_at_all(
    proxy: ProxyHarness,
) -> None:
    """Not an empty heading: a model told there is reference material and shown none is
    being invited to invent a citation."""
    await seed(proxy, "Our office plants are watered on Tuesdays.")
    with_memory(proxy, doc_min_score=0.99)

    response = await send(proxy)

    assert "## Reference material" not in sent_prompt(proxy)
    # Zero, not absent: retrieval looked and found nothing, which is different from not
    # having looked.
    assert response.headers[CHUNKS_HEADER] == "0"


async def test_a_gateway_with_no_connectors_never_searches(proxy: ProxyHarness) -> None:
    await seed(proxy, SECRET_FACT)

    response = await send(proxy)

    assert CHUNKS_HEADER not in response.headers
    assert (await row(proxy)).latency_retrieval_ms is None


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


async def test_a_gateway_cannot_read_a_connector_it_is_not_attached_to(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy, SECRET_FACT, connector=OTHER_CONNECTOR)
    with_memory(proxy)

    await send(proxy)

    assert "Zynthorp" not in sent_prompt(proxy)


async def test_another_organizations_chunks_are_unreachable(proxy: ProxyHarness) -> None:
    """The collection is per tenant, so this is structural — but SPEC §5.3 is the rule
    most worth an explicit test, because a regression here is a disclosure."""
    stranger = uuid.uuid4()
    await proxy.memory.index(stranger, CONNECTOR, SECRET_FACT)
    with_memory(proxy)

    await send(proxy)

    assert "Zynthorp" not in sent_prompt(proxy)


# ---------------------------------------------------------------------------
# failure policy
# ---------------------------------------------------------------------------


class BrokenMemory:
    """A memory service whose retrieval always fails, without a clock or a socket."""

    def __init__(self, outcome: str = "timeout") -> None:
        self.outcome = outcome

    async def recall(self, **kwargs: Any) -> Recall:
        return Recall(
            documents=Retrieval(
                outcome=self.outcome,
                latency_ms=800,
                error="The knowledge base did not answer within 800 ms.",
                query="warranty",
            )
        )

    def injected(self, tokens: int) -> None:
        return None


def break_memory(proxy: ProxyHarness, outcome: str = "timeout") -> None:
    from app.api.proxy.deps import get_memory

    broken = BrokenMemory(outcome)
    proxy.app.dependency_overrides[get_memory] = lambda: broken


async def test_fail_open_still_serves_the_request(proxy: ProxyHarness) -> None:
    with_memory(proxy, on_retrieval_error="fail_open")
    break_memory(proxy)

    response = await send(proxy)

    assert response.status_code == 200
    assert "## Reference material" not in sent_prompt(proxy)


async def test_fail_closed_returns_503_with_a_readable_reason(proxy: ProxyHarness) -> None:
    with_memory(proxy, on_retrieval_error="fail_closed")
    break_memory(proxy)

    response = await send(proxy)

    assert response.status_code == 503
    message = response.json()["error"]["message"]
    assert "documents" in message
    assert "timed out" in message


@pytest.mark.parametrize("outcome", ["timeout", "error"])
async def test_both_failure_kinds_are_governed_by_the_policy(
    proxy: ProxyHarness, outcome: str
) -> None:
    with_memory(proxy, on_retrieval_error="fail_closed")
    break_memory(proxy, outcome)

    assert (await send(proxy)).status_code == 503


async def test_a_failed_retrieval_under_fail_closed_is_still_logged(
    proxy: ProxyHarness,
) -> None:
    """It is the request somebody will come asking about, and the row is where they look."""
    with_memory(proxy, on_retrieval_error="fail_closed")
    break_memory(proxy)

    await send(proxy)

    record = await row(proxy)
    assert record.status_code == 503
    assert record.latency_retrieval_ms == 800


# ---------------------------------------------------------------------------
# headers, budgets and the log
# ---------------------------------------------------------------------------


async def test_the_response_says_how_many_chunks_and_what_retrieval_cost(
    proxy: ProxyHarness,
) -> None:
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    response = await send(proxy)

    assert response.headers[CHUNKS_HEADER] == "1"
    assert int(response.headers[RETRIEVAL_MS_HEADER]) >= 0


async def test_the_log_records_the_chunks_with_their_scores_and_sources(
    proxy: ProxyHarness,
) -> None:
    document_id = await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    await send(proxy)

    entry = (await row(proxy)).retrieved_chunk_ids[0]
    assert entry["source_name"] == "warranty.md"
    assert entry["document_id"] == str(document_id)
    assert entry["injected"] is True
    assert entry["score"] > 0


async def test_the_log_records_what_memory_cost_in_tokens(proxy: ProxyHarness) -> None:
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    await send(proxy)

    assert (await row(proxy)).memory_tokens > 0


async def test_a_chunk_dropped_by_the_budget_is_recorded_as_dropped(
    proxy: ProxyHarness,
) -> None:
    """The interesting failure is not "nothing retrieved" — it is "the right passage was
    retrieved and fell off the end of the budget"."""
    # Two *documents*, not two chunks of one: adjacent chunks of the same document are
    # deduplicated before the budget ever sees them.
    await seed(proxy, SECRET_FACT)
    await seed(proxy, "A second paragraph about Zynthorp warranties and the depot.")
    with_memory(proxy, doc_max_tokens=80)

    await send(proxy)

    entries = (await row(proxy)).retrieved_chunk_ids
    assert any(entry["injected"] for entry in entries)
    assert any(entry.get("dropped") == "doc_max_tokens" for entry in entries)


async def test_the_token_budget_is_never_exceeded(proxy: ProxyHarness) -> None:
    for i in range(6):
        await seed(proxy, f"{SECRET_FACT} paragraph {i}")
    with_memory(proxy, doc_max_tokens=60, doc_top_k=6)

    await send(proxy)

    assert (await row(proxy)).memory_tokens <= 60


async def test_retrieval_latency_lands_on_the_row(proxy: ProxyHarness) -> None:
    await seed(proxy, SECRET_FACT)
    with_memory(proxy)

    await send(proxy)

    assert (await row(proxy)).latency_retrieval_ms is not None


# ---------------------------------------------------------------------------
# streaming, and the interaction with routing
# ---------------------------------------------------------------------------


async def test_a_streamed_request_is_augmented_too(proxy: ProxyHarness) -> None:
    from tests.support import Behaviour
    from tests.support import chunk as sse_chunk

    await seed(proxy, SECRET_FACT)
    with_memory(proxy)
    proxy.upstream.behaviour = Behaviour(chunks=[sse_chunk("hi")])

    response = await proxy.client.post(
        proxy.url(),
        json={**ASK, "stream": True},
        headers=proxy.headers(),
    )

    assert response.status_code == 200
    assert response.headers[CHUNKS_HEADER] == "1"
    assert "Zynthorp" in sent_prompt(proxy)


async def test_retrieval_happens_once_for_a_whole_failover_chain(
    proxy: ProxyHarness,
) -> None:
    """Two targets assemble the same chunks into two prompts; searching twice would
    double the cost of every failover for an identical result."""
    counting = _CountingMemory(proxy.memory.service)
    from app.api.proxy.deps import get_memory

    proxy.app.dependency_overrides[get_memory] = lambda: counting

    await seed(proxy, SECRET_FACT)
    with_memory(proxy)
    proxy.resolver.gateway = replace(
        proxy.gateway,
        routing_mode="failover",
        targets=(proxy.target, replace(proxy.target, name="secondary")),
    )

    await send(proxy)

    assert counting.calls == 1


class _CountingMemory:
    def __init__(self, inner: MemoryService) -> None:
        self._inner = inner
        self.calls = 0

    async def recall(self, **kwargs: Any) -> Recall:
        self.calls += 1
        return await self._inner.recall(**kwargs)

    def injected(self, tokens: int) -> None:
        return None
