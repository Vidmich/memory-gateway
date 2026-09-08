"""Rebuilding every collection under a new embedding model, without a gap in retrieval.

SPEC §9.4's procedure, in order: create the next collection, re-embed into it, verify,
swap the alias, and leave the old one for the sweeper. What makes it worth writing down is
the set of things that had to be decided along the way.

**The setting changes at the swap, not before it.** The obvious design is "PATCH the
embedding model, then reindex", and it is wrong: between the write and the swap every new
ingestion would embed with the new model and upsert into the old collection, which has the
old width, and Qdrant would refuse every one of them. So the operator's confirmation
*starts a run*, the run carries the model it is moving to, and the platform setting is
written as the last step. Until then everything — serving, ingestion, distillation — keeps
using the model that matches the live collection, which is the only self-consistent state
available.

**The text is re-embedded from the index, not from the files.** Every chunk's text is in
its Qdrant payload, because retrieval has to return something a prompt can carry. That
makes a reindex a read of the old collection rather than a re-extraction of a corpus of
PDFs — orders of magnitude cheaper, and it is why the estimate can count what it will cost
instead of guessing. A document whose points are missing entirely is not re-extracted here
either; it is reported, and **Resync** on its connector is the path that exists for that.

**Verification is a count and a search, and it happens before the swap.** The count catches
a copy that stopped early; the search catches a collection that is full of vectors nobody
can rank — a dimension that was accepted and a distance metric that was not what retrieval
expects. Both are cheap, both fail closed, and failing leaves the old collection serving,
which is the entire point of building beside it.

**Points written during the copy are caught up, not raced.** Ingestion carries on while a
reindex runs, so the source grows underneath the scroll. After the first pass the copy runs
again from the beginning until the two counts agree or a bounded number of attempts is
spent. Upserts are keyed by deterministic point ids, so a repeated page costs time and
changes nothing. The residual — a point written between the final count and the swap — is
one document's chunks, and it is recovered by the next ingestion or resync of that
document rather than pretended away.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.core.errors import Conflict, NotFound
from app.core.tenancy import Actor
from app.schemas.platform import EmbeddingChoice, PlatformSettingsPatch, ReindexEstimate
from app.services.embeddings import Embedder
from app.services.maintenance_store import MaintenanceStore
from app.services.platform_settings import PlatformSettingsService
from app.services.reindex_store import (
    EMBEDDING,
    FAILED,
    ORGANIZATION,
    PLATFORM,
    SUCCEEDED,
    SWAPPED,
    VERIFYING,
    ReindexStore,
    RunView,
    TargetView,
)
from app.services.vector_index import COPY_BATCH, VectorIndexAdmin, successor
from app.services.vector_store import ChunkPoint

logger = logging.getLogger(__name__)

#: Characters per token, for the cost estimate. A rule of thumb rather than a tokenizer
#: run over the whole corpus: the number exists so an operator can tell "a few dollars"
#: from "a few thousand", and running a BPE over a million chunks to refine it would cost
#: more than the answer is worth.
CHARS_PER_TOKEN = 4

#: Chunks sampled to estimate mean length. One page, because the estimate's precision is
#: bounded by CHARS_PER_TOKEN anyway.
ESTIMATE_SAMPLE = 256

#: Catch-up passes after the first copy. Bounded so a collection being written to faster
#: than it can be copied fails visibly instead of looping forever.
MAX_CATCH_UP = 3

#: Pause between pages, so a reindex cannot crowd the embedding provider's rate limit or
#: the serving path's share of the vector store.
PAGE_PAUSE_SECONDS = 0.05

#: How close the counts have to be before the swap. Exact: an off-by-one here is a chunk
#: that will never be retrieved again, and there is no reason to accept one.
COUNT_TOLERANCE = 0


class ReindexInProgress(Conflict):
    """A run already covers this scope. Deliberately a 409 rather than a queue: two
    reindexes of one collection would fight over the alias, and the operator's next
    question is always "what is already running", which the message answers."""


@dataclass(frozen=True, slots=True)
class Progress:
    """A run's completion and how long the rest is likely to take."""

    done: int
    total: int
    eta_seconds: int | None

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 1.0


def progress_of(run: RunView, *, now: datetime | None = None) -> Progress:
    """Points done over points expected, plus an ETA at the observed rate.

    ``None`` for the ETA until at least one point has been copied. A number extrapolated
    from nothing is worse than a blank on a screen somebody is using to decide whether to
    wait — and the first seconds of a run are exactly when it would be most wrong.
    """
    total = sum(target.total_points for target in run.targets)
    done = sum(target.done_points for target in run.targets)
    moment = now or datetime.now(UTC)
    elapsed = (moment - run.started_at).total_seconds()
    if done <= 0 or elapsed <= 0 or done >= total:
        return Progress(done=done, total=total, eta_seconds=None)
    rate = done / elapsed
    return Progress(done=done, total=total, eta_seconds=int((total - done) / rate))


class Reindexer:
    """Estimates, starts and runs reindexes. One object, three phases.

    The estimate and the start are control-plane work — a person is waiting — and the run
    is a job. They live together because the estimate has to count exactly what the run
    will copy, and two implementations of "what is in scope" is how an operator ends up
    confirming a cost for one set of collections and paying for another.
    """

    def __init__(
        self,
        store: ReindexStore,
        *,
        index: VectorIndexAdmin,
        maintenance: MaintenanceStore,
        settings: PlatformSettingsService,
        embedder_for: Callable[[EmbeddingChoice], Embedder],
        batch_size: int = COPY_BATCH,
        pause_seconds: float = PAGE_PAUSE_SECONDS,
    ) -> None:
        self._store = store
        self._index = index
        self._maintenance = maintenance
        self._settings = settings
        self._embedder_for = embedder_for
        self._batch = batch_size
        self._pause = pause_seconds

    # -- estimating ------------------------------------------------------

    async def estimate(
        self,
        *,
        choice: EmbeddingChoice | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> ReindexEstimate:
        current = await self._settings.current()
        target = choice or current.embedding
        organizations = await self._scope(organization_id)

        collections: list[str] = []
        points = 0
        characters = 0
        sampled = 0
        for identifier in organizations:
            live = await self._index.live_collection(identifier)
            if live is None:
                continue
            collections.append(live)
            held = await self._index.count_points(live)
            points += held
            if held and sampled < ESTIMATE_SAMPLE:
                page = await self._index.scroll(live, cursor=None, limit=ESTIMATE_SAMPLE)
                for point in page.points:
                    characters += len(str(point.payload.get("text", "")))
                    sampled += 1

        mean = characters / sampled if sampled else 0.0
        return ReindexEstimate(
            collections=sorted(collections),
            organizations=len(collections),
            points=points,
            tokens=int(points * mean / CHARS_PER_TOKEN),
            from_model=current.embedding.name,
            to_model=target.name,
            to_dimension=target.dimension,
        )

    async def _scope(self, organization_id: uuid.UUID | None) -> list[uuid.UUID]:
        if organization_id is not None:
            return [organization_id]
        async with self._maintenance.begin() as transaction:
            return [identifier for identifier, _name in await transaction.organizations()]

    # -- reading ---------------------------------------------------------

    async def find(self, run_id: uuid.UUID) -> RunView | None:
        async with self._store.begin() as transaction:
            return await transaction.run(run_id)

    async def recent(self, *, limit: int = 10) -> list[RunView]:
        async with self._store.begin() as transaction:
            return await transaction.recent(limit=limit)

    async def running(self) -> RunView | None:
        """The run in flight, if there is one.

        Public because two screens ask: Settings, to say that the embedding model it is
        showing is not the one being moved to, and Maintenance, to draw the progress bar.
        """
        async with self._store.begin() as transaction:
            found = await transaction.running()
        return found[0] if found else None

    # -- starting --------------------------------------------------------

    async def start(
        self,
        actor: Actor | None,
        *,
        choice: EmbeddingChoice | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> RunView:
        """Record the decision and lay out the work. Copies nothing.

        Targets are created here rather than in the run so that a run which never starts —
        the worker is down, the queue is backed up — still shows an operator what it is
        going to do and against which collections.
        """
        current = await self._settings.current()
        target = choice or current.embedding

        async with self._store.begin() as transaction:
            for existing in await transaction.running():
                if existing.covers(organization_id):
                    raise ReindexInProgress(
                        "A reindex is already running for this scope. "
                        f"Started {existing.started_at:%Y-%m-%d %H:%M} UTC.",
                    )

            estimate = await self.estimate(choice=target, organization_id=organization_id)
            scope = ORGANIZATION if organization_id is not None else PLATFORM
            run = await transaction.create_run(
                scope=scope,
                organization_id=organization_id,
                from_model=current.embedding.name,
                from_dimension=current.embedding.dimension,
                to_model=target.name,
                to_dimension=target.dimension,
                estimated_points=estimate.points,
                estimated_tokens=estimate.tokens,
                started_by=actor.user_id if actor is not None else None,
            )
            for identifier in await self._scope(organization_id):
                live = await self._index.live_collection(identifier)
                if live is None:
                    # Nothing indexed for this tenant. Skipped rather than given an empty
                    # target: a row reading "0 of 0, swapped" on the progress screen is
                    # noise, and the alias will be created by their first upload.
                    continue
                await transaction.add_target(
                    run.id,
                    organization_id=identifier,
                    collection=successor(identifier, live),
                    total=await self._index.count_points(live),
                )
            await transaction.commit()
            started = await transaction.run(run.id)

        logger.info(
            "reindex started",
            extra={
                "run_id": str(run.id),
                "scope": scope,
                "to_model": target.name,
                "to_dimension": target.dimension,
                "estimated_points": estimate.points,
            },
        )
        assert started is not None
        return started

    # -- running ---------------------------------------------------------

    async def run(self, run_id: uuid.UUID) -> RunView:
        """Do the work. Safe to call again after a crash: every target resumes."""
        async with self._store.begin() as transaction:
            run = await transaction.run(run_id)
        if run is None:
            raise NotFound("No such reindex run.")

        # The provider comes from the live configuration and the model from the run. The
        # run records the model and the width, because those are what a collection has to
        # agree with; the provider is a routing detail that can change under an unchanged
        # model, and pinning it would make a reindex fail after a base-URL move.
        current = await self._settings.current()
        embedder = self._embedder_for(
            EmbeddingChoice(
                provider=current.embedding.provider,
                name=run.to_model,
                dimension=run.to_dimension,
            )
        )
        failures: list[str] = []
        for target in run.targets:
            if target.status == SWAPPED:
                continue
            try:
                await self._rebuild(target, embedder=embedder, dimension=run.to_dimension)
            except Exception as error:
                failures.append(f"{target.organization_id}: {error}")
                logger.exception(
                    "reindex target failed; the old collection is still serving",
                    extra={"run_id": str(run_id), "organization_id": str(target.organization_id)},
                )
                async with self._store.begin() as transaction:
                    await transaction.save_target(target.id, status=FAILED, error=str(error)[:500])
                    await transaction.commit()

        async with self._store.begin() as transaction:
            await transaction.finish_run(
                run_id,
                status=FAILED if failures else SUCCEEDED,
                error="; ".join(failures)[:1000] if failures else None,
            )
            await transaction.commit()

        if not failures:
            # The last step, and the reason the setting was not written earlier: every
            # collection now has the new width, so the model that produces query vectors
            # can finally change to match.
            await self._adopt(run)

        async with self._store.begin() as transaction:
            finished = await transaction.run(run_id)
        assert finished is not None
        return finished

    async def _rebuild(self, target: TargetView, *, embedder: Embedder, dimension: int) -> None:
        organization_id = target.organization_id
        live = await self._index.live_collection(organization_id)
        if live is None or live == target.collection:
            # Either nothing to copy, or a resumed run whose swap already happened. Both
            # are "already done" rather than an error — which is what makes calling this
            # twice safe.
            async with self._store.begin() as transaction:
                await transaction.save_target(target.id, status=SWAPPED)
                await transaction.commit()
            return

        await self._index.create_collection(target.collection, dimension=dimension)
        async with self._store.begin() as transaction:
            await transaction.save_target(target.id, status=EMBEDDING)
            await transaction.commit()

        await self._copy(target, source=live, embedder=embedder)

        for _ in range(MAX_CATCH_UP):
            if await self._index.count_points(target.collection) >= (
                await self._index.count_points(live) - COUNT_TOLERANCE
            ):
                break
            # Points arrived while the scroll was running. Copying from the beginning
            # again is correct and cheap enough: the upserts are keyed by deterministic
            # ids, so everything already there is overwritten with the same value.
            async with self._store.begin() as transaction:
                await transaction.save_target(target.id, clear_cursor=True)
                await transaction.commit()
            await self._copy(replace(target, cursor=None), source=live, embedder=embedder)

        # The count, not the running total: a catch-up pass rewrites points the first pass
        # already wrote, and a sum of what was *sent* would report more than exists.
        done = await self._index.count_points(target.collection)
        async with self._store.begin() as transaction:
            await transaction.save_target(target.id, status=VERIFYING, done_points=done)
            await transaction.commit()
        await self._verify(target, source=live, embedder=embedder)

        await self._index.swap_alias(organization_id, target.collection)
        async with self._store.begin() as transaction:
            await transaction.save_target(target.id, status=SWAPPED, done_points=done)
            await transaction.commit()
        logger.info(
            "reindex swapped an alias",
            extra={
                "organization_id": str(organization_id),
                "from": live,
                "to": target.collection,
                "points": done,
            },
        )

    async def _copy(self, target: TargetView, *, source: str, embedder: Embedder) -> int:
        """Scroll the source, embed each page, upsert it, save the cursor. Resumable.

        The cursor is saved *after* the upsert lands, so a crash costs one repeated page
        rather than a silently skipped one — the same ordering, and the same reasoning, as
        ``distilled_at`` being set after the facts are written.
        """
        cursor = target.cursor
        done = target.done_points
        while True:
            page = await self._index.scroll(source, cursor=cursor, limit=self._batch)
            if page.points:
                await self._index.upsert_into(
                    target.collection, await _embed(page.points, embedder)
                )
                done += len(page.points)
                async with self._store.begin() as transaction:
                    await transaction.save_target(target.id, done_points=done, cursor=page.cursor)
                    await transaction.commit()
            cursor = page.cursor
            if cursor is None:
                return done
            if self._pause:
                await asyncio.sleep(self._pause)

    async def _verify(self, target: TargetView, *, source: str, embedder: Embedder) -> None:
        """Counts, then a search. Raising here leaves the old collection live."""
        expected = await self._index.count_points(source)
        actual = await self._index.count_points(target.collection)
        if actual < expected - COUNT_TOLERANCE:
            raise RuntimeError(
                f"{target.collection} holds {actual} points and {source} holds {expected}"
            )
        if not expected:
            return
        width = await self._index.collection_dimension(target.collection)
        if width != embedder.dimension:
            raise RuntimeError(
                f"{target.collection} was built with width {width}, "
                f"the model produces {embedder.dimension}"
            )
        page = await self._index.scroll(target.collection, cursor=None, limit=1)
        probe = str(page.points[0].payload.get("text", "")) if page.points else ""
        vector = (await embedder.embed([probe or "sample"]))[0]
        found = await self._index.search_in(target.collection, vector, limit=1)
        if not found:
            # A collection with points that answers no query at all is one nothing can
            # retrieve from. Better to fail here, with the old index still serving, than
            # to swap and discover it from a support ticket.
            raise RuntimeError(f"a sample search against {target.collection} returned nothing")

    async def _adopt(self, run: RunView) -> None:
        """Write the embedding the run just made real.

        Only for a platform-wide run. A single organization reindexed onto a different
        model would leave every other tenant's collection at the old width, and a platform
        setting that describes one tenant is a setting that is wrong for the rest.
        """
        if run.scope != PLATFORM:
            return
        current = await self._settings.current()
        if current.embedding.name == run.to_model:
            return
        from app.services.platform_store import platform_attribution

        await self._settings.update(
            platform_attribution("reindex"),
            PlatformSettingsPatch(
                embedding={"name": run.to_model, "dimension": run.to_dimension},
                confirm_reindex=run.to_model,
            ),
        )


async def _embed(points: Sequence[ChunkPoint], embedder: Embedder) -> list[ChunkPoint]:
    texts = [str(point.payload.get("text", "")) for point in points]
    vectors = await embedder.embed(texts)
    return [
        ChunkPoint(id=point.id, vector=list(vector), payload=dict(point.payload))
        for point, vector in zip(points, vectors, strict=True)
    ]


def estimated_cost_lines(estimate: ReindexEstimate) -> list[str]:
    """The estimate as sentences, for a confirmation dialog and for a log line."""
    return [
        f"{estimate.organizations} collection(s): {', '.join(estimate.collections) or 'none'}",
        f"{estimate.points} chunks, about {estimate.tokens} tokens to embed",
        f"{estimate.from_model or 'unset'} to {estimate.to_model} "
        f"({estimate.to_dimension} dimensions)",
    ]


__all__ = [
    "CHARS_PER_TOKEN",
    "MAX_CATCH_UP",
    "Progress",
    "ReindexInProgress",
    "Reindexer",
    "estimated_cost_lines",
    "progress_of",
]
