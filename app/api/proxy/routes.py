"""The OpenAI-compatible data plane: ``/g/{slug}/v1/*``.

The request body is validated here rather than by a FastAPI body parameter. That is
deliberate: FastAPI would raise ``RequestValidationError`` and produce its own envelope,
and a client SDK reading a non-OpenAI error body surfaces an unhelpful generic exception.
Parsing explicitly keeps every failure on this route in the shape callers can read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api.proxy.deps import (
    get_authenticator,
    get_end_users,
    get_limiter,
    get_memory,
    get_proxy_service,
    get_request_logs,
    get_resolver,
    get_router,
)
from app.api.proxy.errors import (
    InvalidRequest,
    ModelNotFound,
    PermissionDenied,
    UnsupportedField,
)
from app.core import background, keys
from app.core.errors import AppError, RateLimited
from app.core.logging import get_request_id
from app.core.tracing import phase
from app.schemas.openai import ChatRequest, ModelCard, ModelList
from app.services.api_keys import AuthenticatedKey, KeyAuthenticator
from app.services.citations import Resolution
from app.services.end_user import EndUserIdentity, resolve_identity, session_key
from app.services.end_user_resolver import EndUserResolver, ResolvedEndUser
from app.services.facts import ANONYMOUS_NOT_ALLOWED, NO_IDENTITY
from app.services.gateway_resolver import GatewayResolver, ResolvedGateway
from app.services.limiter import RateLimiter, RequestLimits
from app.services.proxy import Observers, Prepared, ProxyService
from app.services.request_log import RequestLogService, RequestRecorder, StreamRecorder
from app.services.retrieval import MemoryService, Recall
from app.services.routing import Attempts, Router, RoutingPlan, plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/g/{slug}/v1", tags=["proxy"])

MODEL_HEADER = "X-Gateway-Model"
#: Set to ``off`` to serve a request with no augmentation at all — no retrieval, no
#: injected block, nothing. It exists because the only honest way to measure what memory
#: contributes is to send the same question twice and compare, and doing that by editing
#: the gateway changes the thing being measured for every other caller at the same time.
MEMORY_HEADER = "X-Gateway-Memory"
MEMORY_OFF = "off"
#: How many retrieved chunks made it into the prompt, and what retrieval cost. Present
#: only when retrieval actually ran, so their *absence* means this gateway has no
#: connectors attached or the caller switched memory off — which is itself the answer to
#: "why did it not use my documents".
CHUNKS_HEADER = "X-Gateway-Memory-Chunks"
RETRIEVAL_MS_HEADER = "X-Gateway-Retrieval-Ms"
#: How many durable facts about this caller went into the prompt. Present only when
#: conversation memory actually ran, so its absence answers "why does it not know me" with
#: the same evidence its presence gives: nobody identified the caller, or this gateway has
#: memory switched off.
FACTS_HEADER = "X-Gateway-Memory-Facts"
#: Names the locked parameters that replaced a value the client asked for. Present only
#: when something was actually overridden — a header on every response would be noise,
#: and the one case it matters is a caller wondering why `temperature` had no effect.
LOCKED_HEADER = "X-Gateway-Locked-Params"

Resolver = Annotated[GatewayResolver, Depends(get_resolver)]
Authenticator = Annotated[KeyAuthenticator, Depends(get_authenticator)]
Routing = Annotated[Router, Depends(get_router)]
Logs = Annotated[RequestLogService, Depends(get_request_logs)]
Memory = Annotated[MemoryService, Depends(get_memory)]
EndUsers = Annotated[EndUserResolver, Depends(get_end_users)]
Limiter = Annotated[RateLimiter, Depends(get_limiter)]
Proxy = Annotated[ProxyService, Depends(get_proxy_service)]


@router.post("/chat/completions")
async def chat_completions(
    slug: str,
    request: Request,
    resolver: Resolver,
    authenticator: Authenticator,
    router_: Routing,
    logs: Logs,
    memory: Memory,
    end_users: EndUsers,
    limiter: Limiter,
    proxy: Proxy,
) -> Response:
    """Forward a chat completion, streaming or not."""
    # SPEC §10.5's trace, phase by phase. The spans are opened here rather than deeper
    # down because these are steps of *this* request path, not of the objects that carry
    # them out — a span inside `RateLimiter` would also cover the control plane's use of it.
    with phase("gateway.auth", **{"gateway.slug": slug}):
        key, gateway = await _authorize(request, slug, resolver, authenticator)

    # Recording starts here, once the tenant is known, and covers everything after it —
    # including the 400s. Failures *before* this point are authentication failures, which
    # belong to no organization: there is nobody whose monitoring screen they could
    # honestly appear on, so they stay in the access log and in Prometheus.
    recorder = logs.begin(
        organization_id=gateway.organization_id,
        gateway_id=gateway.id,
        policy=gateway.log_policy,
        api_key_id=key.id,
        request_id=get_request_id(),
        # For the Prometheus label only. The row identifies the gateway by id, which is
        # what survives a rename; a dashboard needs the name somebody would recognise.
        gateway_slug=gateway.slug,
    )

    attempts = Attempts(on_prepared=_record_prompt(recorder))
    # Created before the body is parsed so that the `except` and `finally` below always
    # have something to talk to. It enforces nothing until `requests()` is called, and on
    # a gateway with no limits configured it never touches Redis at all.
    limits = limiter.begin(gateway, end_user_id=None, holder=recorder.record.id.hex)
    # Set once the stream observer owns the concurrency slot, which is the one path where
    # this function returns while the request is still in flight.
    handed_off = False
    try:
        chat = _parse_body(await _read_body(request))
        recorder.client_request(chat)
        if unsupported := chat.unsupported_field():
            raise UnsupportedField(unsupported)

        if chat.model != gateway.virtual_model:
            raise ModelNotFound(
                f"Model '{chat.model}' is not served by gateway '{gateway.slug}'. "
                f"This gateway exposes '{gateway.virtual_model}'."
            )

        # Who is asking (SPEC §6.2). Before routing, because sticky A/B selection hashes
        # the same identity, and before recall, because recall needs the row this creates.
        identity = _identify(chat, request, gateway, key)
        who = await end_users.resolve(organization_id=gateway.organization_id, identity=identity)
        recorder.end_user(
            end_user_id=who.id if who is not None else None,
            session_id=session_key(
                chat.messages,
                request.headers,
                external_id=identity.external_id if identity is not None else None,
            ),
        )

        # SPEC §11's cheap half, before routing, before retrieval, before a single token
        # has been counted: a throttled caller pays for an authentication, an identity
        # lookup and one Redis round trip. Rebuilt rather than mutated because the
        # per-end-user buckets need the row id that only exists now.
        limits = limiter.begin(
            gateway,
            end_user_id=who.id if who is not None else None,
            holder=recorder.record.id.hex,
        )
        with phase("gateway.rate_limit"):
            await limits.requests()

        # Which upstream, and what happens when it does not answer (SPEC §8.1). Resolved
        # before any time is spent so that a misconfigured gateway fails identically in
        # all three modes, and so the attempt list is fixed before the first call.
        #
        # The *explicit* identity only: an IP-derived id moves when somebody changes
        # network, and a caller who slid from A to B mid-experiment is the one thing
        # sticky routing exists to prevent. See `app.services.end_user`.
        routing = plan(gateway, end_user_key=_sticky_key(identity))

        # Memory, once for the whole request — before routing, because every attempt in a
        # failover chain assembles the same retrieved documents into a different prompt.
        with phase("gateway.retrieval") as span:
            recall = await _recall(
                memory, gateway, chat, request.headers, who=who, identity=identity
            )
            if span.is_recording():
                span.set_attribute("memory.chunks", len(recall.documents.chunks))
                span.set_attribute("memory.facts", len(recall.facts))
        recorder.retrieval(latency_ms=recall.latency_ms if recall.attempted else None)
        # SPEC §6.3. Raised here rather than inside the retriever so the editor can render
        # the same failure as a diagnostic instead of a 503.
        recall.enforce(gateway.memory.on_retrieval_error)

        # The expensive half, and it has to be here: SPEC §11 counts *injected* memory,
        # which does not exist until retrieval has run. The concurrency slot is taken in
        # the same atomic check and released the moment the upstream call is over, so it
        # measures calls in flight rather than requests in the building.
        await limits.tokens(_estimate(proxy, chat, gateway, routing, recall, limits))

        recorder.upstream_call_started()
        if chat.stream:
            # Opening the stream sends the request and checks the status *before* any
            # bytes go downstream, so an upstream failure is still an HTTP error rather
            # than a truncated 200 — and, until this returns, it is still a failure the
            # next target can absorb (SPEC §8.2).
            opened = await router_.open_stream(
                chat,
                gateway,
                routing,
                attempts,
                # Ordered: the log's observer fills in the usage the limiter then
                # settles against, and a composite is what makes that ordering a fact
                # rather than a convention.
                observer=Observers((StreamRecorder(recorder), _StreamLimits(limits, recorder))),
                recall=recall,
                # Fires before the observers' `done`, so the row the log's observer
                # submits already says what the answer cited (task 100).
                on_citations=_record_citations(recorder),
            )
            recorder.attempts(attempts.as_json())
            memory.injected(opened.prepared.memory_tokens)
            # Deliberately not submitted here: the observer owns the record from now on
            # and submits it when the stream ends, however it ends. The same is true of
            # the concurrency slot: `_StreamLimits` gives it back from the same `done`
            # callback, which fires on a clean finish, an upstream failure and a client
            # hang-up alike.
            handed_off = True
            return StreamingResponse(
                opened.stream.frames(),
                media_type="text/event-stream",
                headers={
                    **_headers(opened.prepared, recall, limits),
                    "cache-control": "no-cache",
                    # Tells nginx not to buffer the response; without it an ingress can
                    # hold the whole stream and hand the client one lump at the end.
                    "x-accel-buffering": "no",
                },
            )

        completed = await router_.complete(chat, gateway, routing, attempts, recall=recall)
        recorder.attempts(attempts.as_json())
        memory.injected(completed.prepared.memory_tokens)
        # Task 100. Resolved against the attempt that answered, recorded whatever the
        # mode, and applied to the response before it is logged — so the transcript is
        # what the client received, footer and all.
        delivered = proxy.cite(completed.prepared, completed.response)
        _record_citations(recorder)(delivered.resolution)
        recorder.from_response(delivered.response)
        recorder.submit()
        # Inline rather than in the background, because the answer is already built and
        # two Redis round trips on a request that just waited on a provider are not the
        # latency worth optimising — and a settlement that happens deterministically is a
        # settlement that can be tested.
        await limits.release()
        usage = completed.response.usage
        await limits.settle(
            prompt_tokens=usage.prompt_tokens if usage is not None else None,
            completion_tokens=usage.completion_tokens if usage is not None else None,
        )
        return JSONResponse(
            content=delivered.response.model_dump(exclude_none=True),
            headers=_headers(completed.prepared, recall, limits),
        )
    except BaseException as error:
        # Every failure after authorization is somebody's, and the row is the only place
        # they will see it: the client gets an error body and this screen is where they
        # come to ask why. `BaseException` so a cancelled request is recorded too. The
        # attempts go on first: a chain that exhausted itself is the whole explanation,
        # and it lives in the object the raise passed straight through.
        recorder.attempts(attempts.as_json())
        if isinstance(error, RateLimited):
            # SPEC §11 wants throttling visible in monitoring, and task 14 wants the row
            # to be metadata only. The client did send a body; nothing was done with it,
            # and storing a transcript of a request that never reached a model would put
            # end-user text in the log for no reader's benefit.
            recorder.throttled()
        recorder.failed(error)
        recorder.submit()
        if isinstance(error, AppError):
            # Every response carries the budget, refusals included — a client that only
            # learns its limit after exceeding it cannot pace itself. Merged rather than
            # replacing, because `RateLimited` arrives with its own `Retry-After`.
            error.headers = {**limits.headers(), **error.headers}
        raise
    finally:
        # The one slot that has to be given back. Not on the streaming path, where the
        # observer owns it: this function returns while that request is still running.
        if not handed_off:
            await limits.release()


@router.get("/models")
async def list_models(
    slug: str,
    request: Request,
    resolver: Resolver,
    authenticator: Authenticator,
) -> ModelList:
    """The virtual models this gateway exposes, in OpenAI list format."""
    _, gateway = await _authorize(request, slug, resolver, authenticator)
    return ModelList(
        data=[
            ModelCard(
                id=gateway.virtual_model,
                created=int(gateway.created_at.timestamp()),
                owned_by="memory-gateway",
            )
        ]
    )


class _StreamLimits:
    """Releases the concurrency slot and settles tokens when a stream ends.

    A :class:`~app.services.proxy.StreamObserver` because that is the only callback the
    proxy guarantees fires **exactly once however the stream ended** — a clean finish, an
    upstream that died mid-generation, and a client that walked away all arrive here. A
    slot released anywhere else would be a slot leaked in one of those three cases, and a
    leaked concurrency counter is the failure task 14 is explicit about designing against.

    The work is spawned rather than awaited because ``done`` is called from inside the
    body iterator, synchronously, between two frames.
    """

    def __init__(self, limits: RequestLimits, recorder: RequestRecorder) -> None:
        self._limits = limits
        self._recorder = recorder

    def frame(self, frame: Any) -> None:
        """Nothing to do per frame. The token settlement is one correction at the end,
        not an accumulating one — a stream that sends a thousand frames should cost one
        Redis write, not a thousand."""

    def done(self, error: BaseException | None) -> None:
        background.spawn(self._finish(), name="rate-limit-settle")

    async def _finish(self) -> None:
        await self._limits.release()
        # Read off the record, which the log's observer has already filled in from the
        # provider's own usage frame. Several providers send none for a stream, and the
        # settlement correctly does nothing in that case — see `RequestLimits.settle`.
        record = self._recorder.record
        await self._limits.settle(
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
        )


def _estimate(
    proxy: ProxyService,
    chat: ChatRequest,
    gateway: ResolvedGateway,
    routing: RoutingPlan,
    recall: Recall,
    limits: RequestLimits,
) -> int:
    """Prompt tokens for the request about to be sent, injected memory included.

    Assembled a second time, deliberately and only when a token limit actually applies:
    the router assembles per *attempt* and this has to happen before the first attempt
    starts, so there is no result to reuse. It is pure CPU over messages already in
    memory, and it buys the one thing SPEC §11 asks for that a client-side count cannot
    give — the cost of what the gateway added.

    The first target, because that is the one about to be called: a failover to a model
    with a different context window would inject a different amount, and the settlement
    afterwards corrects the difference either way.
    """
    if not limits.needs_estimate or not routing.targets:
        return 0
    return proxy.estimate_tokens(proxy.prepare(chat, gateway, routing.targets[0], recall=recall))


def _headers(prepared: Prepared, recall: Recall, limits: RequestLimits) -> dict[str, str]:
    """Which model answered, what memory added, what this gateway refused to let the
    client change, and how much of its budget is left."""
    headers = {MODEL_HEADER: prepared.target.name, **limits.headers()}
    if prepared.params.overridden:
        # The gateway ignored something the client explicitly asked for. Saying so is the
        # difference between "this endpoint ignores temperature" as a bug report and as a
        # documented policy the caller can read off the response.
        headers[LOCKED_HEADER] = ",".join(prepared.params.overridden)
    if recall.documents.attempted:
        # Zero is a real and useful value here: retrieval looked and found nothing above
        # the score floor. Absent means it never looked.
        headers[CHUNKS_HEADER] = str(prepared.injected_chunks)
    if recall.memory.attempted:
        headers[FACTS_HEADER] = str(prepared.injected_facts)
    if recall.attempted:
        # The wall clock for both halves together, which is the number the caller waited.
        headers[RETRIEVAL_MS_HEADER] = str(recall.latency_ms)
    return headers


def _identify(
    chat: ChatRequest,
    request: Request,
    gateway: ResolvedGateway,
    key: AuthenticatedKey,
) -> EndUserIdentity | None:
    """SPEC §6.2, with the anonymous fallback gated on this gateway's setting."""
    return resolve_identity(
        chat,
        request.headers,
        api_key_id=key.id,
        client_ip=request.client.host if request.client else None,
        allow_anonymous=gateway.memory.allow_anonymous_memory,
    )


def _sticky_key(identity: EndUserIdentity | None) -> str | None:
    if identity is None or identity.anonymous:
        return None
    return identity.external_id


async def _recall(
    memory: MemoryService,
    gateway: ResolvedGateway,
    chat: ChatRequest,
    headers: Mapping[str, str],
    *,
    who: ResolvedEndUser | None,
    identity: EndUserIdentity | None,
) -> Recall:
    """Everything the memory subsystem found, or nothing because the caller said so.

    ``X-Gateway-Memory: off`` short-circuits before the call rather than discarding the
    result afterwards. That is what makes the A/B honest: the comparison is between a
    request that paid for retrieval and one that did not, so the latency difference is
    part of what is being measured. It suppresses **both** halves — documents and facts —
    because the question it exists to answer is "what does memory contribute", and an
    answer that still carried what the gateway knows about this person would not be it.
    """
    if headers.get(MEMORY_HEADER, "").strip().lower() == MEMORY_OFF:
        return Recall()
    return await memory.recall(
        organization_id=gateway.organization_id,
        config=gateway.memory,
        messages=chat.messages,
        end_user_id=who.id if who is not None else None,
        identity_reason=_why_no_identity(identity, gateway),
    )


def _why_no_identity(identity: EndUserIdentity | None, gateway: ResolvedGateway) -> str:
    """Which of the two "nobody to remember" cases this is.

    Only the route can tell them apart, and they need different fixes: one is the
    customer's integration not sending ``X-Gateway-User``, the other is this gateway
    declining to keep memory about an unidentified caller.
    """
    if identity is not None or gateway.memory.allow_anonymous_memory:
        return NO_IDENTITY
    return ANONYMOUS_NOT_ALLOWED


def _record_prompt(recorder: RequestRecorder) -> Callable[[Prepared], None]:
    """Tell the log what each attempt actually sent.

    A closure rather than handing the recorder to the router: routing has no business
    importing the request log, and this is the only thing it would want from it. It also
    means the transcript follows the chain — two targets can carry different system
    contexts, and the prompt worth storing is the one the target that answered received.
    """

    def record(prepared: Prepared) -> None:
        recorder.prepared(
            prepared.request.messages,
            prepared.target,
            # Task 101: our count and its unit, beside the provider's count once it
            # arrives. The estimate is the assembly's own — no second pass.
            tokenizer=prepared.tokenizer_name,
            estimated_tokens=(
                prepared.assembly.prompt_tokens if prepared.assembly is not None else None
            ),
        )
        if prepared.assembly is not None:
            recorder.injected(
                tokens=prepared.assembly.memory_tokens,
                chunks=prepared.assembly.chunk_log(),
                facts=prepared.assembly.fact_log(),
            )

    return record


def _record_citations(recorder: RequestRecorder) -> Callable[[Resolution], None]:
    """Tell the log which injected chunks the answer cited.

    A closure for the same reason :func:`_record_prompt` is one: neither the proxy nor
    the router has any business importing the request log, and ids plus a count are all
    the record wants from a resolution.
    """

    def record(resolution: Resolution) -> None:
        recorder.cited(resolution.cited_ids, unresolved=len(resolution.unresolved))

    return record


async def _authorize(
    request: Request,
    slug: str,
    resolver: GatewayResolver,
    authenticator: Authenticator,
) -> tuple[AuthenticatedKey, ResolvedGateway]:
    """Authenticate, then resolve, then check the key belongs to this gateway.

    Authentication comes first so an unauthenticated caller cannot use the difference
    between 404 and 401 to enumerate which gateway slugs exist.
    """
    token = keys.bearer_token(request.headers.get("authorization"))
    key = await authenticator.authenticate(token)
    gateway = await resolver.resolve(slug)

    if key.gateway_id != gateway.id:
        # Keys are gateway-scoped (SPEC §5.1); a valid key for a different endpoint is a
        # configuration mistake worth naming clearly.
        raise PermissionDenied(f"This API key is not valid for gateway '{slug}'.")

    return key, gateway


async def _read_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        raise InvalidRequest("A JSON request body is required.")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise InvalidRequest(f"Request body is not valid JSON: {exc}") from exc


def _parse_body(payload: Any) -> ChatRequest:
    if not isinstance(payload, dict):
        raise InvalidRequest("Request body must be a JSON object.")
    try:
        return ChatRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        param = ".".join(str(part) for part in first.get("loc", ()))
        raise InvalidRequest(first.get("msg", "Invalid request."), param=param or None) from exc
