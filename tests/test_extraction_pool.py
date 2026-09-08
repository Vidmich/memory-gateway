"""Subprocess isolation for heavy extraction (task 11).

These tests start real processes, which is why there are few of them: what is being
asserted is that the *worker* survives things a thread could not, and there is no way to
assert that without something to survive.

The three cases are the three ways a parser written in C ends a Python process — it
crashes, it never returns, or it allocates until the kernel says no — plus the one that
looks harmless and is not: an exception carrying a reason has to arrive on the other side
still carrying it.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from app.services.extraction import (
    Extracted,
    ExtractionError,
    ExtractorRegistry,
    Section,
    SkippedDocument,
    build_registry,
)
from app.services.extraction_pool import ExtractionPool
from tests.office_fixtures import manual_pdf, scanned_pdf

# Real subprocesses, and `spawn` re-imports the interpreter for each one. On a cold pool
# that is a second or two before any work starts.
pytestmark = pytest.mark.timeout(120)


# ---------------------------------------------------------------------------
# extractors that misbehave
# ---------------------------------------------------------------------------
#
# Module level, and resolved in the child through `build_test_registry` below, because
# `spawn` gives the child nothing but a name to import — a closure or a lambda would not
# survive the trip, which is the same constraint the production path is built around.


def crashing_extractor(data: bytes, *, name: str) -> Extracted:
    """The segfault case, made deterministic. ``os._exit`` leaves no traceback and runs no
    handlers, which is exactly what a parser dying inside C does."""
    os._exit(1)


def hanging_extractor(data: bytes, *, name: str) -> Extracted:
    """The infinite-loop case. A thread doing this could not be stopped at all."""
    time.sleep(300)
    raise AssertionError("unreachable")  # pragma: no cover


def greedy_extractor(data: bytes, *, name: str) -> Extracted:
    """Allocates until something stops it."""
    held = []
    for _ in range(64):
        held.append(bytearray(64 * 1024 * 1024))
    raise AssertionError("unreachable")  # pragma: no cover


def build_test_registry() -> ExtractorRegistry:
    registry = build_registry()
    registry.register(crashing_extractor, media_types=("test/crash",), isolation_key="crash")
    registry.register(hanging_extractor, media_types=("test/hang",), isolation_key="hang")
    registry.register(greedy_extractor, media_types=("test/greedy",), isolation_key="greedy")
    return registry


@pytest.fixture
async def pool() -> object:
    """Generous timeout on purpose: a cold pool pays for spawning an interpreter and
    importing a PDF library before any work starts, and a test that measured *that*
    against the wall clock would be a flaky test about this machine."""
    made = ExtractionPool(workers=1, timeout_seconds=60.0, registry=build_test_registry)
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
async def impatient() -> object:
    """The pool for the two tests that are about the clock. Short, but not so short that
    starting a child could trip it."""
    made = ExtractionPool(workers=1, timeout_seconds=8.0, registry=build_test_registry)
    try:
        yield made
    finally:
        await made.aclose()


# ---------------------------------------------------------------------------


async def test_an_isolated_extraction_returns_the_same_document(pool: ExtractionPool) -> None:
    """The baseline. A subprocess that returns a different answer from the in-process
    reader would be an isolation mechanism that silently changed the product."""
    from app.services.pdf import extract_pdf

    data = manual_pdf(pages=4)

    isolated = await pool.run("pdf", data, name="manual.pdf")

    assert isolated == extract_pdf(data, name="manual.pdf")
    assert isinstance(isolated.sections[0], Section)


async def test_a_crashing_extractor_does_not_take_the_worker_with_it(
    pool: ExtractionPool,
) -> None:
    """The whole reason this module exists. In a thread, this ends the process."""
    with pytest.raises(ExtractionError) as raised:
        await pool.run("crash", b"", name="cursed.pdf")

    assert raised.value.reason == "extraction_crashed"
    assert "very likely corrupt" in str(raised.value)


async def test_the_pool_recovers_after_a_crash(pool: ExtractionPool) -> None:
    """A crash breaks the executor permanently — every later submission fails — so the
    pool has to be rebuilt rather than reused. Without this, one bad file turns every
    subsequent document into a failure and nothing says why."""
    with pytest.raises(ExtractionError):
        await pool.run("crash", b"", name="cursed.pdf")

    extracted = await pool.run("pdf", manual_pdf(pages=3), name="manual.pdf")

    assert extracted.page_count == 3


async def test_an_extraction_that_never_returns_is_stopped(impatient: ExtractionPool) -> None:
    """The cap that a thread cannot have. `asyncio.timeout` around a thread abandons it
    and the CPU keeps burning; around a subprocess the parent can actually kill it."""
    started = time.monotonic()

    with pytest.raises(ExtractionError) as raised:
        await impatient.run("hang", b"", name="loop.pdf")

    assert raised.value.reason == "extraction_timeout"
    assert time.monotonic() - started < 60


async def test_the_pool_recovers_after_a_timeout(impatient: ExtractionPool) -> None:
    with pytest.raises(ExtractionError):
        await impatient.run("hang", b"", name="loop.pdf")

    extracted = await impatient.run("pdf", manual_pdf(pages=3), name="manual.pdf")

    assert extracted.page_count == 3


async def test_a_skip_keeps_its_reason_across_the_process_boundary(
    pool: ExtractionPool,
) -> None:
    """The quiet one. Pickling an exception reconstructs it with ``cls(*args)``, which
    drops a keyword-only field — so without ``__reduce__`` every ``needs_ocr`` decided
    inside a subprocess would arrive as a generic failure, and only for the formats that
    are isolated, which are the ones that needed the reasons."""
    with pytest.raises(SkippedDocument) as raised:
        await pool.run("pdf", scanned_pdf(), name="scan.pdf")

    assert raised.value.reason == "needs_ocr"
    assert "text layer" in str(raised.value)


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is POSIX-only")
async def test_an_extraction_that_allocates_without_bound_is_stopped() -> None:
    """The kernel's limit, not ours. Windows has job objects instead, which are a
    different mechanism with a different lifetime; there the wall clock is the only bound,
    and the pool says so in its log rather than pretending otherwise."""
    pool = ExtractionPool(
        workers=1,
        timeout_seconds=60.0,
        memory_limit_bytes=256 * 1024 * 1024,
        registry=build_test_registry,
    )
    try:
        with pytest.raises(ExtractionError) as raised:
            await pool.run("greedy", b"", name="huge.xlsx")
        assert raised.value.reason in ("extraction_out_of_memory", "extraction_crashed")
    finally:
        await pool.aclose()


async def test_closing_a_pool_that_never_started_is_harmless() -> None:
    """The API process builds one and never uses it. Lazily creating the executor is what
    keeps that free, and closing it has to be free too."""
    await ExtractionPool().aclose()
