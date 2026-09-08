"""The distillation pass, end to end (SPEC §6.4).

One method — :meth:`Distiller.run` — that reads a thread's transcripts, asks a cheap model
what durable facts they contain, and merges the answer into what is already believed. The
pieces it stands on are elsewhere: :mod:`app.services.distillation` builds the prompt and
validates the reply, :mod:`app.services.reconciliation` decides insert versus dedupe versus
supersede, :mod:`app.services.distillation_store` holds the transcripts and the record of
what happened. This module is the order they happen in, and the decisions about *whether*
they happen at all.

**Nothing here can affect a completion.** That is the property, and it is structural rather
than careful: this runs on a worker, minutes after the response was returned, in a process
that a request never waits on. The one place the two touch is the enqueue — see
:mod:`app.services.distillation_trigger` — and it is a fire-and-forget call inside a
background flusher that already swallows its own failures. A distillation model that returns
500 to every call produces dead-lettered jobs and a red line on the memory-health chart, and
nothing else.

**A skip is not a failure, and the difference is recorded.** "The organization switched
distillation off", "the daily cap is spent" and "the model is unreachable" all end with no
facts written, and an operator needs to tell them apart without reading logs. The first
produces no row at all (it is a setting working), the second a ``skipped`` row naming the
cap, the third a ``failed`` row and a retry.

**Caps are checked before the model is called, not after.** A cost guard that discovers it
is over budget by going over budget is a bill, not a guard.

**The transcripts are marked distilled after the facts are written.** A crash in between
costs one repeated pass, which deduplication then absorbs. The other order would silently
skip a conversation, and the transcript would be gone to retention long before anyone
noticed the memory was thin.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import UpstreamStatus
from app.core.metrics import DistillationMetrics
from app.core.tenancy import TenantScope
from app.schemas.distillation import DistillationConfig, organization_distillation
from app.schemas.openai import ChatMessage, ChatRequest, ChatResponse
from app.services.debounce import Debouncer, session_key
from app.services.distillation import (
    MAX_FACTS_IN_PROMPT,
    Exchange,
    Extraction,
    KnownFact,
    MalformedExtraction,
    build_messages,
    exchange_of,
    parse,
)
from app.services.distillation_store import (
    FAILED,
    SKIPPED,
    SUCCEEDED,
    DistillationStore,
    PendingTranscript,
    RunRecord,
    start_of_day,
)
from app.services.end_user_store import EndUserStore
from app.services.jobs import PermanentJobError
from app.services.params import Resolved
from app.services.proxy import Prepared
from app.services.reconciliation import Reconciler

logger = logging.getLogger(__name__)

#: Why a pass did nothing. The three that produce a ``skipped`` row are the ones an
#: operator would otherwise report as "memory has stopped working".
DISABLED = "organization_disabled"
NO_MODEL = "no_distillation_model"
DAILY_CAP = "daily_cap_reached"
USER_CAP = "per_user_cap_reached"
NOTHING_PENDING = "nothing_pending"
TOO_SHORT = "exchange_too_short"
SUPERSEDED_JOB = "superseded_by_a_later_turn"
CONTENDED = "another_pass_holds_this_user"

#: Skips that are worth a row. The rest are the ordinary shape of a quiet system.
RECORDED_SKIPS = frozenset({DAILY_CAP, USER_CAP, NO_MODEL})

#: Ceiling on the extractor's reply. It is one small JSON object; anything approaching this
#: is a model that has started narrating, and paying for the rest of it buys nothing.
MAX_REPLY_TOKENS = 800

#: Dialects whose providers accept OpenAI's JSON mode. Only one exists in this build, and
#: naming the set rather than assuming it is what makes adding task 16's adapter a line
#: here rather than a bug there.
JSON_MODE_DIALECTS = frozenset({"openai"})


@dataclass(frozen=True, slots=True)
class Pass:
    """What one call to :meth:`Distiller.run` did. Returned for the CLI and the tests;
    the durable version is the ``distillation_runs`` row."""

    outcome: str
    reason: str | None = None
    inserted: int = 0
    deduped: int = 0
    superseded: int = 0
    evicted: int = 0
    rejected: int = 0
    candidates: int = 0
    transcripts: int = 0

    @property
    def wrote_anything(self) -> bool:
        return bool(self.inserted or self.deduped or self.superseded)


class Completer(Protocol):
    """The one thing this needs from the proxy: send a prepared request, get a completion.

    A narrow port rather than :class:`~app.services.proxy.ProxyService` itself, so a test
    can script what the extractor "returns" without a socket — which is what makes the
    parser's failure modes (malformed JSON, an out-of-range confidence, a sentence shaped
    like an instruction) testable at all. In production it *is* the proxy: the distillation
    call is an ordinary chat completion, and routing it through the same object means a
    provider quirk is fixed once.
    """

    async def complete(self, prepared: Prepared) -> ChatResponse: ...


class ModelResolver(Protocol):
    async def target(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> UpstreamTarget | None:
        """The model to distil with, or ``None`` when nothing is configured anywhere.

        ``model_id`` is the organization's choice; the implementation falls back to the
        platform default. Returns ``None`` rather than raising, because "nobody has picked a
        distillation model" is a configuration state with a screen, not an incident.
        """
        ...


class Distiller:
    def __init__(
        self,
        store: DistillationStore,
        *,
        end_users: EndUserStore,
        reconciler: Reconciler,
        models: ModelResolver,
        proxy: Completer,
        debouncer: Debouncer,
        metrics: DistillationMetrics | None = None,
    ) -> None:
        self._store = store
        self._end_users = end_users
        self._reconciler = reconciler
        self._models = models
        self._proxy = proxy
        self._debouncer = debouncer
        self._metrics = metrics

    async def run(
        self,
        *,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        session_id: str | None,
        token: str | None = None,
    ) -> Pass:
        """One distillation pass over one thread.

        Raises only for failures worth retrying — an unreachable model, a database that is
        down. Everything a retry cannot fix is a :class:`~app.services.jobs.
        PermanentJobError`, and everything that is simply not worth doing is a skip.
        """
        started = time.perf_counter()
        scope = TenantScope.of_organization(organization_id)

        if token is not None and not await self._debouncer.claim(
            session_key(end_user_id, session_id), token
        ):
            # A later turn armed a newer pass. That one will read this one's transcripts
            # too, so exiting here loses nothing and saves a model call.
            return self._done(Pass(outcome=SKIPPED, reason=SUPERSEDED_JOB), started)

        config = await self._config(scope, organization_id)
        if not config.enabled:
            return self._done(Pass(outcome=SKIPPED, reason=DISABLED), started)

        async with self._store.begin(scope) as transaction:
            pending = list(await transaction.pending(end_user_id, session_id))
        if not pending:
            return self._done(Pass(outcome=SKIPPED, reason=NOTHING_PENDING), started)

        exchange = exchange_of([(entry.request_body, entry.response_body) for entry in pending])
        if not exchange.worth_distilling:
            # Two words and a greeting. Mark them read anyway: leaving them pending would
            # make every later pass re-read them, and re-decide the same thing.
            await self._mark(scope, pending)
            return self._done(Pass(outcome=SKIPPED, reason=TOO_SHORT), started)

        if reason := await self._over_cap(scope, config, end_user_id):
            return self._done(
                await self._record(
                    scope,
                    Pass(outcome=SKIPPED, reason=reason, transcripts=len(pending)),
                    organization_id=organization_id,
                    end_user_id=end_user_id,
                    session_id=session_id,
                    started=started,
                ),
                started,
            )

        target = await self._models.target(organization_id, config.model_id)
        if target is None:
            return self._done(
                await self._record(
                    scope,
                    Pass(outcome=SKIPPED, reason=NO_MODEL, transcripts=len(pending)),
                    organization_id=organization_id,
                    end_user_id=end_user_id,
                    session_id=session_id,
                    started=started,
                ),
                started,
            )

        known = await self._known(scope, end_user_id)
        try:
            extraction = await self._extract(target, exchange, known)
        except MalformedExtraction as exc:
            # Nothing is taken from a reply that was not the requested object. The pass is
            # a *failure* rather than a skip, because a model that cannot follow the schema
            # is a configuration problem somebody has to see — but it is not retried, since
            # sending the same conversation to the same model will produce the same thing.
            logger.warning(
                "distillation model returned something unusable",
                extra={"organization_id": str(organization_id), "model": target.name},
            )
            await self._mark(scope, pending)
            return self._done(
                await self._record(
                    scope,
                    Pass(outcome=FAILED, reason=str(exc), transcripts=len(pending)),
                    organization_id=organization_id,
                    end_user_id=end_user_id,
                    session_id=session_id,
                    started=started,
                    target=target,
                ),
                started,
            )
        except Exception as exc:
            # Unreachable, timed out, 500. Worth retrying, so the transcripts stay pending
            # and the exception leaves this method — the job runner's policy decides how
            # many more times and whether to dead-letter.
            await self._record(
                scope,
                Pass(outcome=FAILED, reason=_summarize(exc), transcripts=len(pending)),
                organization_id=organization_id,
                end_user_id=end_user_id,
                session_id=session_id,
                started=started,
                target=target,
            )
            raise

        applied = await self._reconciler.apply(
            organization_id=organization_id,
            end_user_id=end_user_id,
            candidates=extraction.candidates,
            dedupe_threshold=config.dedupe_threshold,
            max_facts_per_user=config.max_facts_per_user,
            # The newest transcript in the pass. Provenance points at the request whose
            # answer somebody is looking at when they ask where a fact came from, and that
            # is the last one, not the first.
            source_log_id=pending[-1].log_id,
        )
        if applied.contended:
            # Another pass has this user. Its transcripts are not marked, so whichever pass
            # runs next reads them.
            return self._done(Pass(outcome=SKIPPED, reason=CONTENDED), started)

        await self._mark(scope, pending)
        result = Pass(
            outcome=SUCCEEDED,
            inserted=applied.inserted,
            deduped=applied.deduped,
            superseded=applied.superseded,
            evicted=applied.evicted,
            rejected=len(extraction.rejected),
            candidates=extraction.total,
            transcripts=len(pending),
        )
        logger.info(
            "distilled a conversation",
            extra={
                "organization_id": str(organization_id),
                "end_user_id": str(end_user_id),
                "session_id": session_id,
                "inserted": result.inserted,
                "deduped": result.deduped,
                "superseded": result.superseded,
                "rejected": result.rejected,
            },
        )
        return self._done(
            await self._record(
                scope,
                result,
                organization_id=organization_id,
                end_user_id=end_user_id,
                session_id=session_id,
                started=started,
                target=target,
            ),
            started,
        )

    # -- steps ------------------------------------------------------------

    async def _config(self, scope: TenantScope, organization_id: uuid.UUID) -> DistillationConfig:
        async with self._end_users.begin(scope) as transaction:
            return organization_distillation(
                await transaction.organization_settings(organization_id)
            )

    async def _over_cap(
        self, scope: TenantScope, config: DistillationConfig, end_user_id: uuid.UUID
    ) -> str | None:
        midnight = start_of_day()
        async with self._store.begin(scope) as transaction:
            if config.daily_call_cap and await transaction.calls_since(midnight) >= (
                config.daily_call_cap
            ):
                return DAILY_CAP
            if (
                config.per_user_daily_cap
                and await transaction.calls_since(midnight, end_user_id=end_user_id)
                >= config.per_user_daily_cap
            ):
                return USER_CAP
        return None

    async def _known(self, scope: TenantScope, end_user_id: uuid.UUID) -> list[KnownFact]:
        """The facts shown to the extractor so it can supersede rather than duplicate.

        The most recently *observed* ones, which is the same order recall's always-include
        set uses and for the same reason: a contradiction is nearly always with something
        the person said lately, not with something learned two years ago.
        """
        async with self._end_users.begin(scope) as transaction:
            rows = await transaction.recent_facts(
                end_user_id,
                limit=MAX_FACTS_IN_PROMPT,
                min_confidence=0.0,
                now=datetime.now(UTC),
            )
        return [KnownFact(id=str(row.id), text=row.text, kind=row.kind) for row in rows]

    async def _extract(
        self, target: UpstreamTarget, exchange: Exchange, known: Sequence[KnownFact]
    ) -> Extraction:
        messages = build_messages(exchange, list(known))
        raw = await self._complete(target, messages, json_mode=target.dialect in JSON_MODE_DIALECTS)
        return parse(raw, owned_fact_ids=[uuid.UUID(fact.id) for fact in known])

    async def _complete(
        self, target: UpstreamTarget, messages: Sequence[ChatMessage], *, json_mode: bool
    ) -> str:
        payload: dict[str, Any] = {
            "model": target.upstream_model_id,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            # Deterministic on purpose. Two runs over the same conversation should propose
            # the same facts, or the dedupe threshold is being asked to absorb the model's
            # creativity as well as its paraphrasing.
            "temperature": 0.0,
            "max_tokens": MAX_REPLY_TOKENS,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            return _text_of(await self._send(target, payload))
        except UpstreamStatus as exc:
            if not json_mode or not 400 <= exc.status_code < 500:
                raise
            # Plenty of OpenAI-compatible servers — local runtimes especially — reject
            # `response_format` with a 400. One retry without it turns "this provider can
            # never distil" into "this provider distils, with the parse check doing the
            # work JSON mode would have done". Only a 4xx: a 500 or a timeout is the
            # provider being unwell, and asking it the same question again immediately is
            # not a fix, it is the retry policy's job.
            logger.info(
                "the provider refused JSON mode; retrying without it",
                extra={"model": target.name, "dialect": target.dialect},
            )
            payload.pop("response_format", None)
            return _text_of(await self._send(target, payload))

    async def _send(self, target: UpstreamTarget, payload: dict[str, Any]) -> str | None:
        prepared = Prepared(
            request=ChatRequest.model_validate(payload),
            params=Resolved(values={}),
            target=target,
        )
        response = await self._proxy.complete(prepared)
        for choice in response.choices:
            if choice.message is not None and choice.message.content:
                return choice.message.content
        return None

    async def _mark(self, scope: TenantScope, pending: Sequence[PendingTranscript]) -> None:
        async with self._store.begin(scope) as transaction:
            await transaction.mark_distilled(pending, at=datetime.now(UTC))
            await transaction.commit()

    async def _record(
        self,
        scope: TenantScope,
        result: Pass,
        *,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        session_id: str | None,
        started: float,
        target: UpstreamTarget | None = None,
    ) -> Pass:
        if result.outcome == SKIPPED and result.reason not in RECORDED_SKIPS:
            return result
        async with self._store.begin(scope) as transaction:
            await transaction.record(
                RunRecord(
                    organization_id=organization_id,
                    end_user_id=end_user_id,
                    session_id=session_id,
                    outcome=result.outcome,
                    reason=result.reason,
                    model_id=target.id if target is not None else None,
                    model_name=target.name if target is not None else None,
                    transcripts=result.transcripts,
                    candidates=result.candidates,
                    inserted=result.inserted,
                    deduped=result.deduped,
                    superseded=result.superseded,
                    evicted=result.evicted,
                    rejected=result.rejected,
                    duration_ms=_ms(started),
                )
            )
            await transaction.commit()
        return result

    def _done(self, result: Pass, started: float) -> Pass:
        if self._metrics is not None:
            self._metrics.passes.labels(outcome=result.outcome).inc()
            self._metrics.duration.observe(max(0.0, time.perf_counter() - started))
            for disposition, count in (
                ("inserted", result.inserted),
                ("deduped", result.deduped),
                ("superseded", result.superseded),
                ("rejected", result.rejected),
                ("evicted", result.evicted),
            ):
                if count:
                    self._metrics.dispositions.labels(disposition=disposition).inc(count)
        return result


def _text_of(content: str | None) -> str:
    if not content:
        # A completion with no text is not something a retry fixes and not something to
        # take a fact from.
        raise PermanentJobError("the distillation model returned an empty completion")
    return content


def _summarize(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text.splitlines()[0][:400]


def _ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


__all__ = [
    "CONTENDED",
    "DAILY_CAP",
    "DISABLED",
    "JSON_MODE_DIALECTS",
    "MAX_REPLY_TOKENS",
    "NOTHING_PENDING",
    "NO_MODEL",
    "RECORDED_SKIPS",
    "SUPERSEDED_JOB",
    "TOO_SHORT",
    "USER_CAP",
    "Completer",
    "Distiller",
    "ModelResolver",
    "Pass",
]
