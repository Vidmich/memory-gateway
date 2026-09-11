"""The right tokenizer reaches the right consumer (task 101).

Three consumers, three distinguishable doubles. Ingestion is given a tokenizer *source*
and the tests move it under a running pipeline; the proxy is given targets that name
different tokenizers and the tests read which one budgeted; the catalog is given a
request log with both counts on it and the tests read the ratio.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.base import UpstreamTarget
from app.core.ids import uuid7
from app.schemas.openai import ChatMessage, ChatRequest
from app.services.catalog import ModelPatch
from app.services.proxy import ProxyService
from app.services.retrieval import Chunk, Recall, Retrieval
from app.services.tokenizer import ApproximateTokenizer, TiktokenCounter, Tokenizer, WordTokenizer
from app.services.tokenizers import TokenizerSpec
from tests.auth_support import make_organization
from tests.connector_support import build_connectors
from tests.monitoring_support import make_log_row
from tests.support import make_gateway, make_target

MARKDOWN = (
    "Expenses are reimbursed within thirty days. Receipts go through the portal. " * 8
).strip()


class Switchable:
    """A tokenizer source the test can move, standing in for the platform snapshot."""

    def __init__(self, tokenizer: Tokenizer) -> None:
        self.current = tokenizer

    def __call__(self) -> Tokenizer:
        return self.current


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------


async def test_a_document_records_the_tokenizer_it_was_cut_with() -> None:
    source = Switchable(ApproximateTokenizer(3.6))
    fixture = build_connectors(make_organization(), tokenizer=source)

    await fixture.ingest(("handbook.md", MARKDOWN.encode()))
    document = await fixture.document("handbook.md")

    assert document.tokenizer == "approximate:3.6"
    found = await fixture.service.document_chunks(fixture.actor, document.id)
    assert {chunk.payload["tokenizer"] for chunk in found.chunks} == {"approximate:3.6"}


async def test_the_pipeline_reads_the_tokenizer_per_document_not_per_process() -> None:
    """The platform setting moved between two uploads; each row says what cut it."""
    source = Switchable(ApproximateTokenizer(3.6))
    fixture = build_connectors(make_organization(), tokenizer=source)

    await fixture.ingest(("first.md", MARKDOWN.encode()))
    source.current = WordTokenizer()
    await fixture.ingest(("second.md", MARKDOWN.encode()))

    assert (await fixture.document("first.md")).tokenizer == "approximate:3.6"
    assert (await fixture.document("second.md")).tokenizer == "words"


async def test_changing_the_tokenizer_marks_documents_stale_through_the_fingerprint() -> None:
    source = Switchable(ApproximateTokenizer(3.6))
    fixture = build_connectors(make_organization(), tokenizer=source)
    await fixture.ingest(("handbook.md", MARKDOWN.encode()))

    fresh = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert fresh.stale == frozenset()

    source.current = ApproximateTokenizer(3.5)
    stale = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert stale.stale == {row.id for row in stale.items}

    # Recutting under the new unit clears it, and the row now says the new unit.
    await fixture.service.reindex_connector(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()
    again = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert again.stale == frozenset()
    assert (await fixture.document("handbook.md")).tokenizer == "approximate:3.5"


async def test_a_degraded_vocabulary_reaches_the_document_row_by_name() -> None:
    """The acceptance criterion with teeth: a row must not claim a BPE that never loaded."""
    fixture = build_connectors(
        make_organization(), tokenizer=TiktokenCounter("a-vocabulary-that-does-not-exist")
    )

    await fixture.ingest(("handbook.md", MARKDOWN.encode()))

    document = await fixture.document("handbook.md")
    assert document.tokenizer == "words (a-vocabulary-that-does-not-exist unavailable)"


async def test_the_chunking_preview_measures_with_the_current_tokenizer() -> None:
    """Compare and ingestion are one code path; if the setting moves, both move."""
    source = Switchable(ApproximateTokenizer(3.6))
    fixture = build_connectors(make_organization(), tokenizer=source)
    assert fixture.pipeline.tokenizer.name == "approximate:3.6"
    source.current = WordTokenizer()
    assert fixture.pipeline.tokenizer.name == "words"


# ---------------------------------------------------------------------------
# the proxy
# ---------------------------------------------------------------------------


def _chunk(text: str, *, index: int) -> Chunk:
    return Chunk(
        id=f"chunk-{index}",
        score=0.9,
        text=text,
        source_name="handbook.md",
        page_or_section=None,
        document_id=str(uuid7()),
        connector_id=None,
        chunk_index=index,
    )


def _recall(*texts: str) -> Recall:
    chunks = tuple(_chunk(text, index=index) for index, text in enumerate(texts))
    return Recall(documents=Retrieval(chunks=chunks, outcome="hit", latency_ms=1))


def _request() -> ChatRequest:
    return ChatRequest(model="demo", messages=[ChatMessage(role="user", content="hello there")])


def test_assembly_and_the_estimate_use_the_targets_own_tokenizer() -> None:
    proxy = ProxyService(http=None, tokenizer=WordTokenizer())  # type: ignore[arg-type]
    words = make_target("http://a/v1", tokenizer=None)
    dense = make_target("http://b/v1", tokenizer="approximate:1")

    request = _request()
    gateway = make_gateway(words)
    by_words = proxy.prepare(request, gateway, words)
    by_chars = proxy.prepare(request, gateway, dense)

    assert by_words.tokenizer_name == "words"
    assert by_chars.tokenizer_name == "approximate:1"
    # "hello there" is two words and eleven characters.
    assert proxy.estimate_tokens(by_words) == 2
    assert proxy.estimate_tokens(by_chars) == 11
    assert proxy.estimate_tokens(by_chars) == by_chars.assembly.prompt_tokens  # type: ignore[union-attr]


def test_two_gateways_with_the_same_budget_inject_different_amounts() -> None:
    """The acceptance criterion: ``doc_max_tokens`` is the same number in two units, and
    each gateway stays within it *as measured by its own tokenizer*."""
    proxy = ProxyService(http=None, tokenizer=WordTokenizer())  # type: ignore[arg-type]
    # One chunk of ten words; the reference entry adds a header line.
    recall = _recall(*(["alpha bravo charlie delta echo foxtrot golf hotel india juliet"] * 6))

    def injected(target: UpstreamTarget) -> tuple[int, int]:
        gateway = make_gateway(target, memory=_memory(doc_max_tokens=200))
        prepared = proxy.prepare(_request(), gateway, target, recall=recall)
        return prepared.injected_chunks, prepared.memory_tokens

    coarse_chunks, coarse_tokens = injected(make_target("http://a/v1", tokenizer="approximate:8"))
    fine_chunks, fine_tokens = injected(make_target("http://b/v1", tokenizer="approximate:2"))

    assert coarse_chunks > fine_chunks > 0
    assert coarse_tokens <= 200 and fine_tokens <= 200


def _memory(**overrides: object):  # type: ignore[no-untyped-def]
    from app.schemas.gateway_config import MemoryConfig

    return MemoryConfig.model_validate({"connector_ids": [], **overrides})


# ---------------------------------------------------------------------------
# calibration through the catalog
# ---------------------------------------------------------------------------


def _sample(world, *, estimated: int, reported: int | None, **overrides):  # type: ignore[no-untyped-def]
    row = make_log_row(
        world.acme,
        gateway_id=world.acme_gateway.id,
        upstream_model_id=world.acme_model.id,
        model_name=world.acme_model.name,
        tokenizer=overrides.pop("tokenizer", "o200k_base"),
        estimated_prompt_tokens=estimated,
        prompt_tokens=reported,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
        **overrides,
    )
    world.database.request_logs[row.id] = row


async def test_the_calibration_is_reported_over_estimated_across_the_window(directory) -> None:  # type: ignore[no-untyped-def]
    world = directory.world
    _sample(world, estimated=100, reported=110)
    _sample(world, estimated=300, reported=306)
    # A stream whose client never asked for usage: no report, so no sample.
    _sample(world, estimated=1000, reported=None, streamed=True)
    # A failed request reports nothing worth trusting.
    _sample(world, estimated=1000, reported=5, status_code=502)

    rows = await world.catalog.calibrations(world.actor(world.acme_admin))
    found = next(row for row in rows if row.model_id == world.acme_model.id)

    assert found.calibration is not None
    assert (found.calibration.estimated, found.calibration.reported) == (400, 416)
    assert found.calibration.samples == 2
    assert found.calibration.ratio == pytest.approx(1.04)


async def test_samples_taken_under_another_tokenizer_do_not_count(directory) -> None:  # type: ignore[no-untyped-def]
    """An override moves the unit; the window starts again in the new one."""
    world = directory.world
    _sample(world, estimated=100, reported=200, tokenizer="approximate:3.5")

    rows = await world.catalog.calibrations(world.actor(world.acme_admin))
    found = next(row for row in rows if row.model_id == world.acme_model.id)

    assert found.tokenizer.name == "o200k_base"
    assert found.calibration is None


async def test_calibrate_stores_the_measured_ratio_as_the_override(directory) -> None:  # type: ignore[no-untyped-def]
    world = directory.world
    actor = world.actor(world.acme_admin)
    await world.catalog.update_model(
        actor, world.acme_model.id, ModelPatch(tokenizer=TokenizerSpec.approximate(3.5))
    )
    _sample(world, estimated=1000, reported=1040, tokenizer="approximate:3.5")

    view = await world.catalog.calibrate(actor, world.acme_model.id)

    assert view.model.tokenizer == {"name": "approximate", "ratio": pytest.approx(3.365, abs=0.001)}
    # The next window starts in the new unit: nothing has been measured under it yet.
    rows = await world.catalog.calibrations(actor)
    found = next(row for row in rows if row.model_id == world.acme_model.id)
    assert found.tokenizer.key == "approximate:3.365" and found.calibration is None


async def test_a_fixed_vocabulary_cannot_be_calibrated(directory) -> None:  # type: ignore[no-untyped-def]
    from app.core.errors import Validation

    world = directory.world
    _sample(world, estimated=100, reported=120)
    with pytest.raises(Validation, match="fixed vocabulary"):
        await world.catalog.calibrate(world.actor(world.acme_admin), world.acme_model.id)


async def test_the_model_page_shows_the_drift_and_calibrate_applies_it(directory) -> None:  # type: ignore[no-untyped-def]
    world = directory.world
    admin = world.acme_admin
    model = f"/api/v1/models/{world.acme_model.id}"
    patched = await directory.as_user(
        admin, "PATCH", model, json_body={"tokenizer": {"name": "approximate", "ratio": 4.0}}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["tokenizer"] == {"name": "approximate", "ratio": 4.0}
    assert body["effective_tokenizer"]["origin"] == "override"
    assert body["effective_tokenizer"]["label"] == "approximate:4 (override)"

    _sample(world, estimated=500, reported=600, tokenizer="approximate:4")
    listed = await directory.as_user(admin, "GET", "/api/v1/models/calibration")
    row = next(entry for entry in listed.json() if entry["model_id"] == str(world.acme_model.id))
    assert row["ratio"] == pytest.approx(1.2)
    assert row["warns"] is True
    assert row["proposed"] == {"name": "approximate", "ratio": pytest.approx(3.333, abs=0.001)}

    calibrated = await directory.as_user(admin, "POST", f"{model}/calibrate")
    assert calibrated.status_code == 200
    assert calibrated.json()["tokenizer"]["ratio"] == pytest.approx(3.333, abs=0.001)

    # Clearing the override puts the model back on derivation, and the response says so.
    cleared = await directory.as_user(admin, "PATCH", model, json_body={"tokenizer": None})
    assert cleared.json()["tokenizer"] is None
    assert cleared.json()["effective_tokenizer"] == {
        "spec": {"name": "o200k_base", "ratio": None},
        "origin": "derived",
        "name": "o200k_base",
        "label": "o200k_base (derived)",
        "degraded": False,
        "approximate": False,
    }


async def test_a_misspelled_override_is_a_422(directory) -> None:  # type: ignore[no-untyped-def]
    world = directory.world
    response = await directory.as_user(
        world.acme_admin,
        "PATCH",
        f"/api/v1/models/{world.acme_model.id}",
        json_body={"tokenizer": {"name": "cl100k"}},
    )
    assert response.status_code == 422


async def test_the_registry_and_the_derivation_table_are_served(directory) -> None:  # type: ignore[no-untyped-def]
    response = await directory.as_user(directory.world.acme_viewer, "GET", "/api/v1/tokenizers")
    body = response.json()
    assert body["names"] == ["cl100k_base", "o200k_base", "p50k_base", "approximate", "words"]
    assert {"dialect": "openai", "prefix": "gpt-4o", "spec": {"name": "o200k_base", "ratio": None}} in (
        body["derivations"]
    )
    assert body["fallback"] == {"name": "approximate", "ratio": 4.0}


async def test_the_embedding_tokenizer_is_derived_and_overridable(directory) -> None:  # type: ignore[no-untyped-def]
    world = directory.world
    read = await directory.as_user(world.superadmin, "GET", "/api/v1/platform/settings")
    assert read.json()["embedding_tokenizer"]["origin"] == "derived"

    written = await directory.as_user(
        world.superadmin,
        "PATCH",
        "/api/v1/platform/settings",
        json_body={"embedding": {"tokenizer": {"name": "approximate", "ratio": 3.6}}},
    )
    assert written.status_code == 200, written.text
    body = written.json()
    assert body["settings"]["embedding"]["tokenizer"] == {"name": "approximate", "ratio": 3.6}
    assert body["embedding_tokenizer"]["label"] == "approximate:3.6 (override)"
    # The model and dimension did not move, so no reindex run was started.
    assert body["reindex"] is None
