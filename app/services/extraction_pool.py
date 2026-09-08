"""Running a third-party parser where it cannot take anything down with it.

Every extractor before task 11 was ours, over text, and its worst behaviour was slow. The
four added here are large C and C++ libraries reading binary formats designed in the
nineties, driven by files that arrive from the internet. Their failure modes are different
in kind: a crafted PDF can loop inside the parser, a spreadsheet with a hundred thousand
merged cells can allocate until the machine swaps, and a corrupt font table can segfault a
library that has no idea Python exists. A thread cannot be interrupted and a segfault does
not care whose thread it was, so none of that is survivable in the worker process.

So heavy extraction runs in a **subprocess pool**, with the two limits that a thread cannot
have: a wall clock the parent enforces by killing the child, and an address-space ceiling
the kernel enforces. What comes back is a document or an exception; what does not come back
is the worker.

Three things about the shape are deliberate.

**Only a name crosses the boundary.** The parent sends ``"pdf"``, not a function: a closure
does not pickle, and a design where a callable travels is one where anything able to write
to the queue can choose what a worker executes. The child builds its own registry and looks
the name up.

**The context is ``spawn``, everywhere, including Linux.** A forked child of an async worker
inherits the event loop, the open sockets, and the database pool — all of which it then
holds references to and none of which it may use. The startup cost is paid once per child
and the pool is long-lived.

**A crash recycles the whole pool, and that is a real cost.** When a child dies, every
future in flight on that executor fails, not only the one that killed it — so a poisonous
file can collaterally fail a document being read beside it. The alternative is worse: a
crash that raises something retryable leaves the innocent document to be retried until it
dead-letters, with its row stuck in ``extracting`` and nothing on screen saying why.
Failing both means both get a sentence and a **Retry** button, and the retry succeeds for
the one that did nothing wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
from collections.abc import Callable
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor

from app.services.extraction import Extracted, ExtractionError, ExtractorRegistry, build_registry

logger = logging.getLogger(__name__)

#: Subprocesses. Two rather than one so a slow document does not serialise a queue of
#: them, and not many more because each one holds a parser's worth of memory and the
#: worker's own concurrency is already bounded.
DEFAULT_WORKERS = 2

#: Address space one extraction may occupy. Generous for any real document — a 200-page
#: PDF peaks around a tenth of this — and low enough that a file engineered to allocate
#: hits a ceiling long before the machine starts swapping.
DEFAULT_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024

#: Set in each child once, so the imports and the registry are paid for per process rather
#: than per document.
_REGISTRY: ExtractorRegistry | None = None


class ExtractionPool:
    """A process pool that knows how to be killed.

    Lazy: the executor is not created until the first heavy file arrives, so the API
    process — which builds the whole pipeline in order to delete documents and reconcile
    connectors — never starts a subprocess it has no use for.
    """

    def __init__(
        self,
        *,
        workers: int = DEFAULT_WORKERS,
        timeout_seconds: float = 120.0,
        memory_limit_bytes: int | None = DEFAULT_MEMORY_LIMIT_BYTES,
        registry: Callable[[], ExtractorRegistry] = build_registry,
    ) -> None:
        self._workers = max(1, workers)
        self._timeout = timeout_seconds
        self._memory_limit = memory_limit_bytes
        #: How a child builds the registry it resolves names against. A *function*, not a
        #: registry: the child is a separate interpreter, so what travels is the name of
        #: something it can import and call for itself.
        self._registry = registry
        self._executor: ProcessPoolExecutor | None = None
        self._lock = asyncio.Lock()

    async def run(self, key: str, data: bytes, *, name: str) -> Extracted:
        """Extract one document in a child process, or raise something readable."""
        loop = asyncio.get_running_loop()
        executor = await self._ensure()
        future = loop.run_in_executor(executor, _extract, key, data, name)
        try:
            async with asyncio.timeout(self._timeout):
                return await future
        except TimeoutError as exc:
            await self._recycle("extraction timed out", name)
            raise ExtractionError(
                f"Reading this file took longer than {int(self._timeout)} seconds and "
                "was stopped. It may be very large, or its internal structure may be "
                "pathological.",
                reason="extraction_timeout",
            ) from exc
        except MemoryError as exc:
            # Raised inside the child by the address-space limit and pickled back here.
            # The child survived, so there is nothing to recycle.
            raise ExtractionError(
                "Reading this file needed more memory than one document is allowed. It "
                "may be very large, or it may be malformed in a way that makes a reader "
                "allocate without bound.",
                reason="extraction_out_of_memory",
            ) from exc
        except BrokenExecutor as exc:
            await self._recycle("extraction crashed the reader", name)
            raise ExtractionError(
                "The reader for this file crashed. The file is very likely corrupt; if "
                "it opens correctly elsewhere, this is worth reporting.",
                reason="extraction_crashed",
            ) from exc

    async def aclose(self) -> None:
        """Stop the children. Called from the worker's shutdown and the API's lifespan."""
        async with self._lock:
            self._shutdown()

    # -- internals -------------------------------------------------------

    async def _ensure(self) -> ProcessPoolExecutor:
        async with self._lock:
            if self._executor is None:
                self._executor = ProcessPoolExecutor(
                    max_workers=self._workers,
                    # See the module docstring: `spawn` on every platform, not only the
                    # one where it is the default.
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_child,
                    initargs=(self._memory_limit, self._registry),
                )
            return self._executor

    async def _recycle(self, why: str, name: str) -> None:
        """Kill the children and drop the executor; the next call builds a new one.

        ``shutdown`` alone would wait for the very task that has to be stopped, so the
        processes are terminated first. There is no public API for that — a pool that can
        be killed is not something ``concurrent.futures`` offers — so this reaches for the
        private collection, guarded, because the alternative is a wall clock that cannot
        actually stop anything.
        """
        logger.warning(why, extra={"file": name})
        async with self._lock:
            self._shutdown()

    def _shutdown(self) -> None:
        executor, self._executor = self._executor, None
        if executor is None:
            return
        for process in list(getattr(executor, "_processes", {}).values()):
            with contextlib.suppress(Exception):  # pragma: no cover - already dead
                process.terminate()
        executor.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# the child side
# ---------------------------------------------------------------------------


def _child(memory_limit_bytes: int | None, registry: Callable[[], ExtractorRegistry]) -> None:
    """Run once per subprocess, before it takes any work."""
    _limit_memory(memory_limit_bytes)
    global _REGISTRY
    _REGISTRY = registry()


def _limit_memory(limit: int | None) -> None:
    """Cap the child's address space where the platform has such a thing.

    POSIX only. Windows has job objects, which are a different mechanism with a different
    lifetime and are not worth carrying for a development platform — so the wall clock is
    the only limit there, and this says so once rather than pretending otherwise.
    """
    if limit is None:
        return
    try:
        import resource
    except ImportError:  # pragma: no cover - platform-dependent
        logger.info("no address-space limit on this platform; the time limit still applies")
        return
    # Reached by name rather than as attributes: the module exists on macOS and Linux but
    # not every build of it carries `RLIMIT_AS`, and a type checker running on Windows
    # cannot see either of them at all.
    setrlimit = getattr(resource, "setrlimit", None)
    address_space = getattr(resource, "RLIMIT_AS", None)
    if setrlimit is None or address_space is None:  # pragma: no cover - platform-dependent
        return
    try:  # pragma: no cover - exercised only on POSIX
        setrlimit(address_space, (limit, limit))
    except (ValueError, OSError) as exc:  # pragma: no cover - a hard limit already lower
        logger.info("could not set the extraction memory limit", extra={"error": str(exc)})


def _extract(key: str, data: bytes, name: str) -> Extracted:
    """The whole of what a child process does."""
    if _REGISTRY is None:  # pragma: no cover - the initializer always runs first
        raise ExtractionError("This worker started without a registry.")
    extractor = _REGISTRY.by_isolation_key(key)
    if extractor is None:  # pragma: no cover - the parent looked the key up from the same
        raise ExtractionError(f"No isolated extractor is registered as {key!r}.")
    return extractor(data, name=name)


__all__ = [
    "DEFAULT_MEMORY_LIMIT_BYTES",
    "DEFAULT_WORKERS",
    "ExtractionPool",
]
