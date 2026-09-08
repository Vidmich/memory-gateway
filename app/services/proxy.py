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
from collections.abc import AsyncIterator
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
from app.schemas.openai import ChatRequest, ChatResponse, StreamFrame
from app.services.gateway_resolver import ResolvedGateway
from app.services.params import Resolved, resolve_params
from app.services.prompt import Assembled, assemble, prompt_tokens
from app.services.retrieval import Recall
from app.services.sse import DONE, format_event
from app.services.tokenizer import Tokenizer, WordTokenizer

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

    @property
    def memory_tokens(self) -> int:
        return self.assembly.memory_tokens if self.assembly is not None else 0

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
    ) -> None:
        self._http = http
        # The same tokenizer the chunker used, so the budget a gateway sets in tokens is
        # measured in the same tokens the index was built with. A different one here would
        # make `doc_max_tokens` mean something slightly different from `chunk_size`, which
        # is the sort of discrepancy nobody finds by reading.
        self._tokenizer = tokenizer or WordTokenizer()

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
            tokenizer=self._tokenizer,
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
        )

    def estimate_tokens(self, prepared: Prepared) -> int:
        """Prompt tokens for an assembled request, injected memory included (SPEC §11).

        Measured with the *same* tokenizer :meth:`prepare` budgeted with, which is what
        makes a gateway's ``tokens_per_minute`` and its ``doc_max_tokens`` two numbers in
        one unit rather than two units with one name.

        An estimate, and not apologetically so: the provider's tokenizer is its own, and
        the only exact count is the one it reports afterwards. Task 14 settles against
        that; this is what has to be known *before* the request is sent, which no exact
        method can give.
        """
        return prompt_tokens(prepared.request.messages, tokenizer=self._tokenizer)

    # -- non-streaming -------------------------------------------------------

    async def complete(self, prepared: Prepared) -> ChatResponse:
        target = prepared.target
        adapter = _adapter_for(target)
        outbound = _outbound(adapter, prepared)

        try:
            response = await self._http.send(outbound)
        except httpx.TimeoutException as exc:
            raise _timeout(target, exc) from exc
        except httpx.HTTPError as exc:
            raise _unreachable(target, exc) from exc

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

    # -- streaming -----------------------------------------------------------

    async def open_stream(
        self, prepared: Prepared, *, observer: StreamObserver | None = None
    ) -> UpstreamStream:
        """Start the upstream call and validate its status. Nothing is yielded yet."""
        target = prepared.target
        adapter = _adapter_for(target)
        outbound = _outbound(adapter, prepared)

        try:
            response = await self._http.send(outbound, stream=True)
        except httpx.TimeoutException as exc:
            raise _timeout(target, exc) from exc
        except httpx.HTTPError as exc:
            raise _unreachable(target, exc) from exc

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
    ) -> None:
        self._response = response
        self._adapter = adapter
        self._request = request
        self._observer = observer
        self.target = target

    async def frames(self) -> AsyncIterator[str]:
        outcome: BaseException | None = None
        try:
            async for frame in self._adapter.parse_stream(self._response, self._request):
                # Observed before it is yielded, so a tee sees every frame the client
                # sees and no frame the client does not.
                self._notify(frame)
                yield format_event(frame.data)
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

    def _notify(self, frame: StreamFrame) -> None:
        if self._observer is None:
            return
        try:
            self._observer.frame(frame)
        except Exception:  # pragma: no cover - an observer that raises is a bug
            logger.warning("stream observer failed on a frame", exc_info=True)

    def _finish(self, error: BaseException | None) -> None:
        if self._observer is None:
            return
        try:
            self._observer.done(error)
        except Exception:  # pragma: no cover - never at the client's expense
            logger.warning("stream observer failed at the end of a stream", exc_info=True)


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
