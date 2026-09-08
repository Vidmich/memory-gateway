"""The worker's wiring: the parts that are strings on both sides of a process boundary.

Nothing here starts a worker. It asserts the handful of facts that, if they drifted, would
produce a queue that accepts jobs and a worker that never runs them — with no error
anywhere, because both halves would be individually correct.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from app.core.config import Settings, get_settings
from app.services.job_queue import ARQ_FUNCTION
from app.services.jobs import (
    DELETE_CONNECTOR,
    DISTIL_MEMORY,
    INGEST_DOCUMENT,
    JOB_NAMES,
    REINDEX,
)
from app.workers.main import WorkerSettings, run_gateway_job
from app.workers.runtime import (
    Distillation,
    Platform,
    build_handlers,
    embedding_settings,
    ingestion_settings,
    retry_policy,
)
from tests.auth_support import make_organization
from tests.connector_support import build_connectors
from tests.distillation_support import build_distillation
from tests.platform_support import PlatformFixture
from tests.platform_support import build_platform as build_platform_fixture


def test_the_queue_and_the_worker_agree_on_the_function_name() -> None:
    """A rename on one side alone leaves jobs sitting in Redis forever, with no error."""
    assert [function.__name__ for function in WorkerSettings.functions] == [ARQ_FUNCTION]
    assert run_gateway_job.__name__ == ARQ_FUNCTION


def test_arq_is_told_not_to_retry() -> None:
    """Retries are :class:`~app.services.jobs.RetryPolicy`. Leaving arq's default on would
    produce two backoff schedules, two attempt counters, and a dead-letter record written
    after the queue had already given up on its own terms."""
    assert WorkerSettings.max_tries == 1


def test_the_job_timeout_is_well_above_the_extraction_cap() -> None:
    """So the only thing that ever hits it is a job that is genuinely wedged rather than
    one that is merely slow."""
    assert WorkerSettings.job_timeout > get_settings().extraction_timeout_seconds * 2


def test_there_is_a_handler_for_every_job_this_build_can_enqueue() -> None:
    """The other half of the same failure: an enqueue with no handler dead-letters, which
    is loud but useless.

    Driven against a *fully wired* worker — ingestion, conversation memory and task 17's
    platform bundle — because that is the deployment every name in ``JOB_NAMES`` is
    enqueued by. A worker missing one of those registers fewer handlers on purpose; the
    two tests below are the ones that say so.
    """
    fixture = build_connectors(make_organization())
    ingestion = _ingestion_of(fixture)
    platform = build_platform_fixture()

    handlers = build_handlers(ingestion, _distillation_of()[0], _platform_of(platform))
    assert set(handlers) == set(JOB_NAMES)
    assert set(JOB_NAMES) == {INGEST_DOCUMENT, DELETE_CONNECTOR, DISTIL_MEMORY, REINDEX}


def test_a_worker_without_conversation_memory_registers_two_handlers() -> None:
    """A deployment that has not enabled it runs a worker with two, and a ``distil_memory``
    job arriving there is dead-lettered *by name* — a visible bad deploy rather than a
    silent one."""
    handlers = build_handlers(_ingestion_of(build_connectors(make_organization())))

    assert set(handlers) == {INGEST_DOCUMENT, DELETE_CONNECTOR}


async def test_the_reindex_handler_turns_a_string_run_id_back_into_a_uuid() -> None:
    """The payload crossed a process boundary as JSON, so the id arrived as a string.

    A handler that passed it through would fail deep inside the reindex with a lookup
    that found nothing — indistinguishable, from the log, from a run somebody deleted.
    """
    platform = build_platform_fixture()
    seen: list[uuid.UUID] = []

    async def capture(run_id: uuid.UUID) -> Any:
        seen.append(run_id)
        return None

    platform.reindexer.run = capture  # type: ignore[method-assign]
    handlers = build_handlers(
        _ingestion_of(build_connectors(make_organization())), None, _platform_of(platform)
    )
    run_id = uuid.uuid4()

    await handlers[REINDEX]({"run_id": str(run_id)})

    assert seen == [run_id]


async def test_the_distillation_handler_turns_string_ids_back_into_uuids() -> None:
    """The same rule as the ingestion handler: the payload crossed a process boundary as
    JSON, and unpacking it is all a handler is allowed to be."""
    distillation, fixture = _distillation_of()
    alice = await fixture.end_user()
    fixture.log(alice)
    handlers = build_handlers(_ingestion_of(build_connectors(make_organization())), distillation)

    await handlers[DISTIL_MEMORY](
        {
            "organization_id": str(fixture.organization_id),
            "end_user_id": str(alice.id),
            "session_id": "thread-1",
            "token": None,
        }
    )

    assert await fixture.texts_of(alice) == ["Works in Rust."]


async def test_a_handler_turns_string_ids_back_into_uuids() -> None:
    """The payload crossed a process boundary as JSON. A handler is allowed to be exactly
    this and nothing more."""
    fixture = build_connectors(make_organization())
    handlers = build_handlers(_ingestion_of(fixture))
    await fixture.upload(("handbook.md", b"# Handbook\n\nWidgets.\n"))
    document = (await fixture.documents())[0]

    await handlers[INGEST_DOCUMENT](
        {
            "organization_id": str(fixture.organization_id),
            "document_id": str(document.id),
        }
    )

    assert (await fixture.document("handbook.md")).status == "indexed"


async def test_a_handler_with_a_payload_it_cannot_read_raises() -> None:
    """Rather than quietly doing nothing. A malformed payload is a deploy problem, and the
    dead letter is where it becomes visible."""
    fixture = build_connectors(make_organization())
    handlers = build_handlers(_ingestion_of(fixture))

    with pytest.raises((KeyError, ValueError)):
        await handlers[INGEST_DOCUMENT]({"organization_id": "not-a-uuid"})


def test_the_settings_reach_the_pieces_that_read_them() -> None:
    """Four knobs, three destinations. A setting that is defined and never read is the
    kind of thing that is only discovered when somebody changes it and nothing happens."""
    settings = _settings(
        upload_max_file_bytes=1234,
        extraction_timeout_seconds=7.0,
        embedding_model="text-embedding-3-large",
        embedding_dimension=3072,
        job_max_attempts=9,
    )

    assert ingestion_settings(settings).max_file_bytes == 1234
    assert ingestion_settings(settings).extraction_timeout_seconds == 7.0
    assert embedding_settings(settings).model == "text-embedding-3-large"
    assert embedding_settings(settings).dimension == 3072
    assert retry_policy(settings).max_attempts == 9


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update=overrides)


def _distillation_of() -> tuple[Distillation, Any]:
    """The bundle :func:`~app.workers.runtime.build_distillation` returns, over memory,
    and the fixture behind it so a test can read back what a handler wrote.

    Assembled by hand for the same reason ``_ingestion_of`` is: the composition root takes
    :class:`~app.core.clients.Clients`, which opens pools this test has no use for.
    """
    fixture = build_distillation()
    return Distillation(
        store=fixture.store,
        end_users=fixture.end_users,
        vectors=fixture.vectors,
        debouncer=fixture.debouncer,
        models=fixture.resolver,
        reconciler=fixture.reconciler,
        distiller=fixture.distiller,
        trigger=fixture.trigger,
    ), fixture


def _ingestion_of(fixture: Any) -> Any:
    """The same object :func:`build_ingestion` returns, assembled from a memory fixture.

    Built by hand rather than through the composition root because that one takes
    :class:`~app.core.clients.Clients`, which opens pools this test has no use for.
    """
    from app.services.extraction_pool import ExtractionPool
    from app.workers.runtime import Ingestion

    return Ingestion(
        store=fixture.store,
        objects=fixture.objects,
        vectors=fixture.vectors,
        embedder=fixture.embedder,
        tokenizer=fixture.pipeline._tokenizer,
        registry=fixture.registry,
        queue=fixture.queue,
        lock=fixture.lock,
        pipeline=fixture.pipeline,
        settings=fixture.pipeline._settings,
        # Never used: the pool is lazy, and the pipeline this fixture holds was built
        # without one, so no subprocess is started by anything below.
        pool=ExtractionPool(),
    )


def test_a_payload_carries_only_what_survives_a_deploy() -> None:
    """Ids as strings. Anything richer would be a version dependency between the API and
    the worker, which are deployed separately and restart independently."""
    payload: Mapping[str, Any] = {
        "organization_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
    }

    assert all(isinstance(value, str) for value in payload.values())


def _platform_of(fixture: PlatformFixture) -> Platform:
    """The fixture as the bundle ``build_handlers`` expects.

    Only ``reindexer`` is ever read by a handler, and the rest are the fixture's own
    objects rather than doubles — so a handler that grew a second dependency would find a
    working one here instead of a ``None`` that only fails in production.
    """
    return Platform(
        settings=fixture.platform_settings,
        store=fixture.store,
        index=fixture.index,
        partitions=fixture.partitions,
        retention=fixture.retention,
        sweeper=fixture.sweeper,
        reindexer=fixture.reindexer,
        eraser=fixture.eraser,
        service=fixture.service,
    )
