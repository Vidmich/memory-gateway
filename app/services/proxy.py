"""Forwarding a chat completion to an upstream model.

Two shapes, one pipeline. The non-streaming path is a request/response; the streaming path
splits into "send and check the status" and "relay frames", because once the first byte of
an SSE body has been written the HTTP status is already on the wire and an error can no
longer be reported as one. Validating before returning the body iterator is what makes a
provider 429 come back to the client as a 429.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.adapters.base import (
    DialectRejected,
    MalformedUpstreamResponse,
    UpstreamAdapter,
    UpstreamStreamFailed,
    UpstreamTarget,
    get_adapter,
)
from app.api.proxy.errors import (
    GatewayUnavailable,
    InvalidRequest,
    UpstreamStatus,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from app.core.tracing import phase, record_error
from app.schemas.openai import ChatRequest, ChatResponse, StreamFrame
from app.services.citations import (
    MODE_OFF,
    Delivered,
    Resolution,
    StreamCitations,
    Wrapping,
    deliver,
)
from app.services.gateway_resolver import ResolvedGateway
from app.services.params import Resolved, resolve_params
from app.services.prompt import Assembled, assemble, prompt_tokens
from app.services.retrieval import Chunk, Recall
from app.services.sse import DONE, format_event
from app.services.templates import DEFAULT_TEMPLATES, Templates
from app.services.tokenizer import Tokenizer, WordTokenizer
from app.services.tokenizers import resolve

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Prepared:
    """A request that has been through prompt assembly and the parameter merge.

    Carries the merge result alongside the payload because the two are read at different
    moments: the request goes upstream, the ``overridden`` list goes into a response
    header that has to be written before any body.

    ``assembly`` is the third of those moments. It is the account of what memory did —
    which chunks went in, which were dropped and why, how many tokens it cost — and it is
    per *attempt* rather than per request, because two targets can have different context
    windows and therefore inject different amounts. The header and the log row have to
    describe the attempt that answered, not the first one tried.
    """

    request: ChatRequest
    params: Resolved
    target: UpstreamTarget
    assembly: Assembled | None = None
    #: For ``{gateway}`` in a prefix or suffix. The name, not the slug: it is prose.
    gateway_name: str = ""
    #: How the gateway wants citations delivered (task 100). Carried here rather than
    #: read off the gateway again at delivery time, for the same reason the assembly is:
    #: the response is handled after the request was prepared, and the two must agree.
    citations: str = MODE_OFF
    #: The gateway's templates (task 105), carried for the same reason as the mode: the
    #: footer and the answer wrapping are rendered after the request was prepared, and
    #: the two must agree on the wording.
    templates: Templates = DEFAULT_TEMPLATES

    @property
    def template_fingerprint(self) -> str:
        return self.templates.fingerprint

    @property
    def wrapping(self) -> Wrapping:
        """What goes around the answer: the gateway's prefix and suffix, with the names
        they may print. Inactive — adding no frame — when both are empty."""
        return Wrapping(templates=self.templates, gateway=self.gateway_name, model=self.target.name)

    @property
    def injected(self) -> tuple[Chunk, ...]:
        """The chunks the prompt numbered, in the order it numbered them — what a
        citation resolves against."""
        return self.assembly.injected if self.assembly is not None else ()

    @property
    def memory_tokens(self) -> int:
        return self.assembly.memory_tokens if self.assembly is not None else 0

    @property
    def tokenizer_name(self) -> str | None:
        """What every count on this attempt was measured with (task 101)."""
        return self.assembly.tokenizer if self.assembly is not None else None

    @property
    def injected_chunks(self) -> int:
        return len(self.assembly.injected) if self.assembly is not None else 0

    @property
    def injected_facts(self) -> int:
        return len(self.assembly.injected_facts) if self.assembly is not None else 0


class StreamObserver(Protocol):
    """Someone watching a stream go past, without being able to change it.

    Two methods and no imports: this is how the request log tees a streamed response
    without :class:`ProxyService` knowing that request logging exists. An
    observer must not raise and must not block — it is called between reading a frame
    from the provider and writing it to the client, which is the tightest loop in the
    system.
    """

    def frame(self, frame: StreamFrame) -> None: ...

    def done(self, error: BaseException | None) -> None:
        """Called exactly once, whatever ended the stream — including a client hang-up,
        which arrives as :class:`asyncio.CancelledError`."""


@dataclass(frozen=True, slots=True)
class Observers:
    """Several observers as one, called in order.

    Order is the point rather than a detail: task 14's limiter settles its token estimate
    against the usage the request log's observer has just extracted from the final frame,
    so the two are not independent and a set would be the wrong container.

    Each is isolated from the others — one that raises does not stop the rest — because
    the alternative is a bug in an observer becoming a truncated stream for a client.
    """

    members: tuple[StreamObserver, ...]

    def frame(self, frame: StreamFrame) -> None:
        for member in self.members:
            try:
                member.frame(frame)
            except Exception:  # pragma: no cover - an observer that raises is a bug
                logger.warning("stream observer failed on a frame", exc_info=True)

    def done(self, error: BaseException | None) -> None:
        for member in self.members:
            try:
                member.done(error)
            except Exception:  # pragma: no cover - never at the client's expense
                logger.warning("stream observer failed at the end of a stream", exc_info=True)


class ProxyService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        tokenizer: Tokenizer | None = None,
        ui_base_url: str | None = None,
    ) -> None:
        self._http = http
        # The fallback for a target that names no tokenizer — one built by hand, outside
        # the resolver. Since task 101 the real answer comes from the target: the model's
        # own tokenizer, derived from its dialect and id or overridden in the catalog, so
        # `doc_max_tokens` and `tokens_per_minute` are measured in the unit the provider
        # bills in rather than in whichever one the process happened to load.
        self._tokenizer = tokenizer or WordTokenizer()
        #: Where the control plane's chunk inspector lives, for the link a citation
        #: carries. ``None`` means citations are delivered without one.
        self._ui_base_url = ui_base_url

    # -- request construction ------------------------------------------------

    def prepare(
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        target: UpstreamTarget,
        *,
        recall: Recall | None = None,
    ) -> Prepared:
        """Apply the prompt layers and the parameter merge, once.

        The result is still a ``ChatRequest``: the adapter's job starts at wire format,
        not at policy, and a future ``tools`` field threads through here untouched. It is
        a separate step from sending because the caller needs the merge *before* the
        response exists — the locked-parameter header goes out with the status line, and
        on a stream that is before the first token.

        ``recall`` is what the memory subsystem found, already retrieved. It is passed in
        rather than fetched here because retrieval is one network call for the whole
        request while this function runs once per routing attempt — searching again for
        the second target would double the cost of a failover for an identical result.
        """
        with phase("gateway.assembly", **{"upstream.model": target.name}) as span:
            prepared = self._assemble(request, gateway, target, recall)
            if span.is_recording():
                span.set_attribute("memory.tokens", prepared.memory_tokens)
            return prepared

    def _assemble(
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        target: UpstreamTarget,
        recall: Recall | None,
    ) -> Prepared:
        memory = gateway.memory
        assembly = assemble(
            request.messages,
            model_context=target.system_context,
            gateway_context=gateway.system_context,
            chunks=recall.documents.chunks if recall is not None else (),
            facts=recall.facts if recall is not None else (),
            doc_max_tokens=memory.doc_max_tokens,
            memory_max_tokens=memory.memory_max_tokens,
            context_window=target.context_window,
            tokenizer=self.tokenizer_for(target),
            templates=gateway.templates,
        )
        params = resolve_params(
            model_defaults=target.default_params,
            gateway_overrides=gateway.param_overrides,
            client=request.client_parameters(),
            locked=gateway.locked_params,
        )

        payload: dict[str, Any] = request.model_dump(exclude_none=True)
        payload.update(params.values)
        payload["messages"] = [
            message.model_dump(exclude_none=True) for message in assembly.messages
        ]
        return Prepared(
            request=ChatRequest.model_validate(payload),
            params=params,
            target=target,
            assembly=assembly,
            citations=memory.citations,
            templates=gateway.templates,
            gateway_name=gateway.name,
        )

    def tokenizer_for(self, target: UpstreamTarget) -> Tokenizer:
        """The tokenizer a target's counts are measured with (task 101).

        The resolver put the registry key on the target when it built the payload;
        resolving it is a cached lookup. A target without one — built outside the
        resolver — gets the process fallback, which is what every target got before.
        """
        return resolve(target.tokenizer) if target.tokenizer else self._tokenizer

    def estimate_tokens(self, prepared: Prepared) -> int:
        """Prompt tokens for an assembled request, injected memory included (SPEC §11).

        Measured with the *same* tokenizer :meth:`prepare` budgeted with — read off the
        assembly rather than counted again, so the two cannot disagree — which is what
        makes a gateway's ``tokens_per_minute`` and its ``doc_max_tokens`` two numbers in
        one unit rather than two units with one name.

        An estimate, and not apologetically so: the provider's tokenizer is its own, and
        the only exact count is the one it reports afterwards. Task 14 settles against
        that; this is what has to be known *before* the request is sent, which no exact
        method can give. Task 101 records the two side by side and shows the gap.
        """
        if prepared.assembly is not None:
            return prepared.assembly.prompt_tokens
        return prompt_tokens(
            prepared.request.messages, tokenizer=self.tokenizer_for(prepared.target)
        )

    # -- non-streaming -------------------------------------------------------

    async def complete(self, prepared: Prepared) -> ChatResponse:
        target = prepared.target
        adapter = _adapter_for(target)
        outbound = _outbound(adapter, prepared)

        # The span covers the send only, not the parse: it is the provider's time, and
        # the whole point of the trace is to say how much of a slow request was theirs.
        with phase("upstream.request", **_upstream_attributes(target, stream=False)) as span:
            try:
                response = await self._http.send(outbound)
            except httpx.TimeoutException as exc:
                record_error(span, exc)
                raise _timeout(target, exc) from exc
            except httpx.HTTPError as exc:
                record_error(span, exc)
                raise _unreachable(target, exc) from exc
            if span.is_recording():
                span.set_attribute("http.response.status_code", response.status_code)

        # A non-streaming send has already read and closed the body.
        _raise_for_upstream_status(response, target, adapter)
        try:
            return adapter.parse(response)
        except MalformedUpstreamResponse as exc:
            logger.warning(
                "upstream returned an unparseable completion",
                extra={"model": target.name, "error": str(exc)},
            )
            raise GatewayUnavailable(
                f"[upstream:{target.name}] returned a response that is not a chat completion."
            ) from exc

    def cite(self, prepared: Prepared, response: ChatResponse) -> Delivered:
        """Resolve the answer's citations against what this attempt injected (task 100).

        Always, whatever the gateway's mode: under ``off`` the response comes back the
        same object, untouched, and only the resolution — which the request log records
        — is new. The chunks are the *attempt's*, because two targets can have injected
        different amounts and ``[3]`` means whatever the prompt that answered said it did.
        """
        return deliver(
            response,
            prepared.injected,
            mode=prepared.citations,
            base_url=self._ui_base_url,
            templates=prepared.templates,
            wrapping=prepared.wrapping,
        )

    # -- streaming -----------------------------------------------------------

    async def open_stream(
        self,
        prepared: Prepared,
        *,
        observer: StreamObserver | None = None,
        on_citations: Callable[[Resolution], None] | None = None,
    ) -> UpstreamStream:
        """Start the upstream call and validate its status. Nothing is yielded yet.

        ``on_citations`` is told what the answer cited once the stream ends, however it
        ends — the same guarantee observers have, and it fires before their ``done`` so
        the record is complete by the time the log's observer submits it.
        """
        target = prepared.target
        adapter = _adapter_for(target)
        outbound = _outbound(adapter, prepared)

        # Closed once the status is known, which is what this phase measures:
        # time-to-first-byte. The frames that follow are relayed as they arrive and belong
        # to no span — a span that stayed open for the whole generation would report the
        # length of the answer rather than the latency of the provider.
        with phase("upstream.stream", **_upstream_attributes(target, stream=True)) as span:
            try:
                response = await self._http.send(outbound, stream=True)
            except httpx.TimeoutException as exc:
                record_error(span, exc)
                raise _timeout(target, exc) from exc
            except httpx.HTTPError as exc:
                record_error(span, exc)
                raise _unreachable(target, exc) from exc
            if span.is_recording():
                span.set_attribute("http.response.status_code", response.status_code)

        if response.status_code >= 400:
            try:
                await response.aread()
                _raise_for_upstream_status(response, target, adapter)
            finally:
                await response.aclose()

        return UpstreamStream(
            response=response,
            adapter=adapter,
            target=target,
            request=prepared.request,
            observer=observer,
            # Built only when something was injected or the gateway wraps the answer
            # (task 105): with nothing to resolve against and nothing to add there is
            # nothing to do, and a stream with no citation stage in it is the exact
            # code path task 18 measured.
            citations=(
                StreamCitations(
                    prepared.injected,
                    mode=prepared.citations,
                    base_url=self._ui_base_url,
                    templates=prepared.templates,
                    wrapping=prepared.wrapping,
                )
                if prepared.injected or prepared.templates.wraps
                else None
            ),
            on_citations=on_citations,
        )


class UpstreamStream:
    """Relays SSE frames downstream as they arrive, with no buffering in between."""

    def __init__(
        self,
        *,
        response: httpx.Response,
        adapter: UpstreamAdapter,
        target: UpstreamTarget,
        request: ChatRequest,
        observer: StreamObserver | None = None,
        citations: StreamCitations | None = None,
        on_citations: Callable[[Resolution], None] | None = None,
    ) -> None:
        self._response = response
        self._adapter = adapter
        self._request = request
        self._observer = observer
        self._citations = citations
        self._on_citations = on_citations
        self.target = target

    async def frames(self) -> AsyncIterator[str]:
        outcome: BaseException | None = None
        try:
            async for frame in self._adapter.parse_stream(self._response, self._request):
                # Observed before it is yielded, so a tee sees every frame the client
                # sees and no frame the client does not — including the frames the
                # citation stage rewrites or adds, which is what makes the stored
                # transcript the text the client actually received.
                for outbound in self._cite(frame):
                    self._notify(outbound)
                    yield format_event(outbound.data)
            for outbound in self._citations.finish() if self._citations is not None else ():
                self._notify(outbound)
                yield format_event(outbound.data)
            yield format_event(DONE)
        except asyncio.CancelledError as exc:
            # The client hung up. Closing the response below cancels the upstream call so
            # the provider stops generating — and stops charging for — tokens nobody will
            # read.
            outcome = exc
            logger.info(
                "client disconnected mid-stream; cancelling upstream",
                extra={"model": self.target.name},
            )
            raise
        except (httpx.TimeoutException, httpx.HTTPError, UpstreamStreamFailed) as exc:
            # The status line went out with the first frame, so this cannot become a 504.
            # SPEC §8.2: terminate the stream with an error event instead. A provider that
            # reports a failure *inside* its own stream — Anthropic's `error` event — lands
            # here too: nothing is wrong with the connection, but the remedy is identical
            # and the client has already had a 200.
            outcome = exc
            logger.warning(
                "upstream stream failed after it had started",
                extra={"model": self.target.name, "error": type(exc).__name__},
            )
            yield format_event(_stream_error(self.target, exc))
        finally:
            # In `finally` rather than after the loop so a hang-up, a provider failure and
            # a clean finish all produce exactly one log row. Without it, the requests
            # most worth having a record of are the ones that would not have one.
            self._finish(outcome)
            await self._response.aclose()

    def _cite(self, frame: StreamFrame) -> list[StreamFrame]:
        if self._citations is None:
            return [frame]
        try:
            return self._citations.feed(frame)
        except Exception:  # pragma: no cover - a rewrite that raises must not cost a frame
            logger.warning("citation stage failed on a frame; relaying it as is", exc_info=True)
            return [frame]

    def _notify(self, frame: StreamFrame) -> None:
        if self._observer is None:
            return
        try:
            self._observer.frame(frame)
        except Exception:  # pragma: no cover - an observer that raises is a bug
            logger.warning("stream observer failed on a frame", exc_info=True)

    def _finish(self, error: BaseException | None) -> None:
        # Citations first, observers second: the log's observer submits the record from
        # its `done`, and the record has to already say what was cited.
        if self._citations is not None and self._on_citations is not None:
            try:
                self._on_citations(self._citations.resolution())
            except Exception:  # pragma: no cover - never at the client's expense
                logger.warning("could not record citations for a stream", exc_info=True)
        if self._observer is None:
            return
        try:
            self._observer.done(error)
        except Exception:  # pragma: no cover - never at the client's expense
            logger.warning("stream observer failed at the end of a stream", exc_info=True)


def _upstream_attributes(target: UpstreamTarget, *, stream: bool) -> dict[str, Any]:
    """What a span may say about an upstream call.

    The model, the dialect and whether it streamed. Deliberately not the base URL and
    never a header: a trace is exported to a third-party backend, and SPEC §5.4's rule
    that a credential never leaves this process does not stop being true because the
    destination is an observability vendor.
    """
    return {"upstream.model": target.name, "upstream.dialect": target.dialect, "stream": stream}


def _adapter_for(target: UpstreamTarget) -> UpstreamAdapter:
    try:
        return get_adapter(target.dialect)
    except ValueError as exc:
        raise GatewayUnavailable(
            f"Upstream model '{target.name}' uses dialect '{target.dialect}', "
            f"which this build does not support."
        ) from exc


def _timeout(target: UpstreamTarget, exc: Exception) -> UpstreamTimeout:
    logger.warning(
        "upstream timed out",
        extra={"model": target.name, "timeout_seconds": target.timeout_seconds},
    )
    return UpstreamTimeout(
        f"[upstream:{target.name}] did not respond within {target.timeout_seconds}s."
    )


def _unreachable(target: UpstreamTarget, exc: Exception) -> UpstreamUnavailable:
    # `str(exc)` on a transport error can contain the full URL; the model name is enough
    # for the caller, and the detail goes to the log.
    logger.warning(
        "upstream connection failed",
        extra={"model": target.name, "error": type(exc).__name__},
    )
    return UpstreamUnavailable(f"[upstream:{target.name}] could not be reached.")


def _outbound(adapter: UpstreamAdapter, prepared: Prepared) -> httpx.Request:
    """Build the provider's request, or turn a refusal into a 400.

    A dialect that cannot express what was asked — ``n: 3`` against a provider that
    returns one completion — says so here, before anything is sent. Translated into the
    client-facing error at this layer rather than raised as one from the adapter, because
    an adapter that imported the data plane's exception hierarchy would be a seam pointing
    the wrong way.
    """
    try:
        return adapter.prepare(prepared.request, prepared.target)
    except DialectRejected as exc:
        raise InvalidRequest(exc.message, param=exc.param) from exc


def _raise_for_upstream_status(
    response: httpx.Response, target: UpstreamTarget, adapter: UpstreamAdapter
) -> None:
    """Relay a provider error with its own status, type and code.

    Preserving them is what lets a client SDK raise ``RateLimitError`` for an upstream 429
    instead of a generic server error — which is the difference between a caller that
    backs off and one that retries immediately.

    The *adapter* decides what those four fields are, because a provider's own status is
    not always the one the client should act on: Anthropic answers "come back in a moment"
    with a 529, which SPEC §8.2's retry table has never heard of and would therefore treat
    as final.
    """
    if response.status_code < 400:
        return

    failure = adapter.error(response)
    raise UpstreamStatus(
        status_code=failure.status_code,
        model_name=target.name,
        message=failure.message,
        upstream_type=failure.type,
        upstream_code=failure.code,
        param=failure.param,
    )


def _stream_error(target: UpstreamTarget, exc: Exception) -> str:
    kind = "upstream_timeout" if isinstance(exc, httpx.TimeoutException) else "upstream_error"
    return json.dumps(
        {
            "error": {
                "message": f"[upstream:{target.name}] the stream ended before it completed.",
                "type": "server_error",
                "param": None,
                "code": kind,
            }
        }
    )
