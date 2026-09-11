"""The summarization phase, end to end (task 102).

One method — :meth:`Summarizer.summarize` — that takes one document's extracted text, asks
a cheap model what it is about, and returns what happened. The pieces it stands on are
elsewhere: :mod:`app.services.summarization` builds the prompt and reads the reply,
:mod:`app.services.summarization_store` keeps the ledger, and the pipeline decides what a
result *means* for the document — because that depends on the mode, and the mode is the
pipeline's business.

**Caps are checked before the model is called, not after.** A cost guard that discovers it
is over budget by going over budget is a bill, not a guard. Counted per connector from the
ledger, the same way task 13 counts its cap from ``distillation_runs``.

**A refusal is not a failure, and the ledger says which.** The cap biting and the model
being unconfigured produce ``skipped`` rows; a provider that answered with a 400, an empty
completion or a page of nothing produces a ``failed`` row and no retry, because the same
document to the same model produces the same thing. A provider that is *unwell* — 429,
5xx, a timeout — produces a ``failed`` row and an exception, so the job's backoff does its
work. That split is the module-level rule of :mod:`app.services.ingestion` applied to one
more phase.

**The tokens recorded are the provider's.** When ``usage`` is on the reply, that is what
the row carries. When it is not, the estimate is stored and ``estimated`` is set, because a
blank on a bill is worse than a number with a stated error.

**Which model** is the same rule as distillation's, with one more link in the chain: the
connector's own choice, then the organization's summarization default, then the
organization's distillation model, then the platform default. The resolver here composes
:class:`~app.services.distillation_models.CatalogModelResolver` rather than copying it —
the decryption, the disabled check and the deleted-model fallback are all its.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import UpstreamStatus
from app.core.metrics import SummarizationMetrics
from app.core.tenancy import TenantScope
from app.schemas.distillation import organization_distillation
from app.schemas.openai import ChatRequest, ChatResponse
from app.schemas.summarization import SummarizationConfig, organization_summarization
from app.services.distillation_models import CatalogModelResolver, ModelChoice
from app.services.distillation_store import start_of_day
from app.services.end_user_store import EndUserStore
from app.services.params import Resolved
from app.services.proxy import Prepared
from app.services.summarization import (
    PROMPT_VERSION,
    MalformedSummary,
    build_messages,
    excerpt,
    parse_summary,
    token_estimate,
)
from app.services.summarization_store import (
    DAILY_CAP,
    FAILED,
    NO_MODEL,
    SKIPPED,
    SUCCEEDED,
    RunRecord,
    SummarizationStore,
)
from app.services.tokenizer import Tokenizer

logger = logging.getLogger(__name__)

#: ``SummaryOutcome.status`` values. ``capped`` and ``failed`` are the two the pipeline
#: treats differently by mode; ``summarized`` is the happy path.
SUMMARIZED = "summarized"
CAPPED = "capped"
FAILED_SUMMARY = "failed"

#: Machine-readable reasons on a failed summary. ``reason`` on the ledger row and on the
#: document row are the same string, so the panel and the table agree.
PROVIDER_REFUSED = "provider_refused"
MALFORMED = "malformed_summary"

#: HTTP statuses from the provider that a retry might change. Everything else in the 4xx
#: range is the provider saying no to this request, which it will say again.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})


class Completer(Protocol):
    """The one thing this needs from the proxy: send a prepared request, get a completion.
    The same port :mod:`app.services.distiller` names, for the same reason."""

    async def complete(self, prepared: Prepared) -> ChatResponse: ...


class SummaryModels(Protocol):
    async def target(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> UpstreamTarget | None: ...

    async def describe(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> ModelChoice | None: ...


@dataclass(frozen=True, slots=True)
class ModelSource:
    """Which link in the chain a resolved model came from, for the Settings screen."""

    choice: ModelChoice
    source: str  # "connector" | "summarization" | "distillation" | "platform"


class SummarizationModelResolver:
    """The connector's choice, the organization's, the distillation model's, the platform's.

    Composes :class:`CatalogModelResolver`, whose own fallback covers the last link: passing
    it ``None`` resolves the platform default, and passing it an id that has since been
    deleted falls back to the platform default with a warning.
    """

    def __init__(self, catalog: CatalogModelResolver, *, settings: EndUserStore) -> None:
        self._catalog = catalog
        self._settings = settings

    async def target(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> UpstreamTarget | None:
        chosen, _ = await self._chain(organization_id, model_id)
        return await self._catalog.target(organization_id, chosen)

    async def describe(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> ModelChoice | None:
        chosen, _ = await self._chain(organization_id, model_id)
        return await self._catalog.describe(organization_id, chosen)

    async def explain(self, organization_id: uuid.UUID) -> ModelSource | None:
        """What a connector with no choice of its own would use, and from which link."""
        chosen, source = await self._chain(organization_id, None)
        choice = await self._catalog.describe(organization_id, chosen)
        if choice is None:
            return None
        if choice.from_platform:
            source = "platform"
        return ModelSource(choice=choice, source=source)

    async def _chain(
        self, organization_id: uuid.UUID, model_id: uuid.UUID | None
    ) -> tuple[uuid.UUID | None, str]:
        if model_id is not None:
            return model_id, "connector"
        settings = await self._organization_settings(organization_id)
        default = organization_summarization(settings).model_id
        if default is not None:
            return default, "summarization"
        distils_with = organization_distillation(settings).model_id
        if distils_with is not None:
            return distils_with, "distillation"
        return None, "platform"

    async def _organization_settings(self, organization_id: uuid.UUID) -> Mapping[str, Any]:
        async with self._settings.begin(TenantScope.of_organization(organization_id)) as tx:
            return await tx.organization_settings(organization_id)


@dataclass(frozen=True, slots=True)
class SummaryOutcome:
    """What one attempt produced. The pipeline turns it into a document state."""

    status: str
    summary: str | None = None
    #: The machine-readable half of ``error``; the ledger row carries the same string.
    reason: str | None = None
    #: A sentence for the document row, naming the model where there is one.
    error: str | None = None
    model_id: uuid.UUID | None = None
    model_name: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    estimated: bool = False
    prompt_version: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == SUMMARIZED


class Summarizer:
    def __init__(
        self,
        store: SummarizationStore,
        *,
        models: SummaryModels,
        proxy: Completer,
        metrics: SummarizationMetrics | None = None,
    ) -> None:
        self._store = store
        self._models = models
        self._proxy = proxy
        self._metrics = metrics

    @property
    def models(self) -> SummaryModels:
        return self._models

    async def summarize(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        source_name: str,
        text: str,
        tokenizer: Tokenizer,
        config: SummarizationConfig,
    ) -> SummaryOutcome:
        """One attempt at one document, under its *effective* configuration.

        Raises only for a provider failure a retry might fix; the row for that attempt is
        written first, so the retry counts against the cap and the panel sees it.
        """
        started = time.perf_counter()
        scope = TenantScope.of_organization(organization_id)

        if config.daily_document_cap is not None and await self._over_cap(
            scope, connector_id, config.daily_document_cap
        ):
            outcome = SummaryOutcome(
                status=CAPPED,
                reason=DAILY_CAP,
                error=(
                    f"This connector has summarized its {config.daily_document_cap} documents "
                    "for today; the rest resume after midnight UTC."
                ),
            )
            return await self._finish(
                scope, outcome, SKIPPED, organization_id, connector_id, document_id, started
            )

        target = await self._models.target(organization_id, config.model_id)
        if target is None:
            outcome = SummaryOutcome(
                status=FAILED_SUMMARY,
                reason=NO_MODEL,
                error=(
                    "No summarization model is configured: pick one on the connector, under "
                    "Settings, or as the platform default."
                ),
            )
            return await self._finish(
                scope, outcome, SKIPPED, organization_id, connector_id, document_id, started
            )

        sent = excerpt(text, tokenizer, max_input_tokens=config.max_input_tokens)
        messages = build_messages(
            sent.text, max_summary_tokens=config.max_summary_tokens, name=source_name
        )
        estimate_in = sum(
            token_estimate(str(message.content or ""), tokenizer) for message in messages
        )

        try:
            response = await self._proxy.complete(
                Prepared(
                    request=ChatRequest.model_validate(
                        {
                            "model": target.upstream_model_id,
                            "messages": [m.model_dump(exclude_none=True) for m in messages],
                            # Deterministic: two ingestions of the same bytes should get
                            # the same summary, or the reuse rule is the only thing
                            # keeping the prefix stable.
                            "temperature": 0.0,
                            "max_tokens": config.max_summary_tokens,
                        }
                    ),
                    params=Resolved(values={}),
                    target=target,
                )
            )
            summary = parse_summary(_content_of(response))
        except MalformedSummary as exc:
            outcome = SummaryOutcome(
                status=FAILED_SUMMARY,
                reason=MALFORMED,
                error=f"{target.name} returned nothing usable as a summary: {exc}.",
                model_id=target.id,
                model_name=target.name,
                tokens_in=estimate_in,
                estimated=True,
            )
            return await self._finish(
                scope, outcome, FAILED, organization_id, connector_id, document_id, started
            )
        except UpstreamStatus as exc:
            if _retryable(exc):
                await self._record_and_raise(
                    scope, exc, target, organization_id, connector_id, document_id, started
                )
            outcome = SummaryOutcome(
                status=FAILED_SUMMARY,
                reason=PROVIDER_REFUSED,
                error=f"{target.name} refused the summarization request: {_summarize(exc)}",
                model_id=target.id,
                model_name=target.name,
                tokens_in=estimate_in,
                estimated=True,
            )
            return await self._finish(
                scope, outcome, FAILED, organization_id, connector_id, document_id, started
            )
        except Exception as exc:
            # Unreachable, timed out, 500. The world is bad, not the document.
            await self._record_and_raise(
                scope, exc, target, organization_id, connector_id, document_id, started
            )

        usage = response.usage
        reported = usage is not None and (usage.prompt_tokens or usage.completion_tokens)
        outcome = SummaryOutcome(
            status=SUMMARIZED,
            summary=summary,
            model_id=target.id,
            model_name=target.name,
            tokens_in=int(usage.prompt_tokens) if reported and usage else estimate_in,
            tokens_out=(
                int(usage.completion_tokens)
                if reported and usage
                else token_estimate(summary, tokenizer)
            ),
            estimated=not reported,
            prompt_version=PROMPT_VERSION,
        )
        logger.info(
            "document summarized",
            extra={
                "document_id": str(document_id),
                "model": target.name,
                "tokens_in": outcome.tokens_in,
                "tokens_out": outcome.tokens_out,
                "estimated": outcome.estimated,
                "truncated": sent.truncated,
            },
        )
        return await self._finish(
            scope, outcome, SUCCEEDED, organization_id, connector_id, document_id, started
        )

    # -- steps ------------------------------------------------------------

    async def _over_cap(self, scope: TenantScope, connector_id: uuid.UUID, cap: int) -> bool:
        async with self._store.begin(scope) as transaction:
            used = await transaction.documents_since(connector_id, start_of_day())
        return used >= cap

    async def _finish(
        self,
        scope: TenantScope,
        outcome: SummaryOutcome,
        row_outcome: str,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        started: float,
    ) -> SummaryOutcome:
        await self._record(
            scope,
            RunRecord(
                organization_id=organization_id,
                connector_id=connector_id,
                document_id=document_id,
                outcome=row_outcome,
                reason=outcome.reason,
                model_id=outcome.model_id,
                model_name=outcome.model_name,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                estimated=outcome.estimated,
                duration_ms=_ms(started),
            ),
        )
        return outcome

    async def _record_and_raise(
        self,
        scope: TenantScope,
        error: BaseException,
        target: UpstreamTarget,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        started: float,
    ) -> NoReturn:
        await self._record(
            scope,
            RunRecord(
                organization_id=organization_id,
                connector_id=connector_id,
                document_id=document_id,
                outcome=FAILED,
                reason=_summarize(error),
                model_id=target.id,
                model_name=target.name,
                duration_ms=_ms(started),
            ),
        )
        raise error

    async def _record(self, scope: TenantScope, run: RunRecord) -> None:
        async with self._store.begin(scope) as transaction:
            await transaction.record(run)
            await transaction.commit()
        if self._metrics is not None:
            self._metrics.runs.labels(outcome=run.outcome).inc()
            self._metrics.duration.observe(max(0.0, run.duration_ms / 1000))
            if run.tokens_in or run.tokens_out:
                model = run.model_name or "unknown"
                self._metrics.tokens.labels(direction="in", model=model).inc(run.tokens_in)
                self._metrics.tokens.labels(direction="out", model=model).inc(run.tokens_out)


def _content_of(response: ChatResponse) -> str | None:
    for choice in response.choices:
        if choice.message is not None and choice.message.content:
            return choice.message.content
    return None


def _retryable(error: UpstreamStatus) -> bool:
    return error.status_code >= 500 or error.status_code in RETRYABLE_STATUSES


def _summarize(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text.splitlines()[0][:400]


def _ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


__all__ = [
    "CAPPED",
    "FAILED_SUMMARY",
    "MALFORMED",
    "PROVIDER_REFUSED",
    "RETRYABLE_STATUSES",
    "SUMMARIZED",
    "Completer",
    "ModelSource",
    "SummarizationModelResolver",
    "Summarizer",
    "SummaryModels",
    "SummaryOutcome",
]
