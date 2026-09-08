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
    get_memory,
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
from app.core import keys
from app.core.logging import get_request_id
from app.schemas.openai import ChatRequest, ModelCard, ModelList
from app.services.api_keys import AuthenticatedKey, KeyAuthenticator
from app.services.end_user import EndUserIdentity, resolve_identity, session_key
from app.services.end_user_resolver import EndUserResolver, ResolvedEndUser
from app.services.facts import ANONYMOUS_NOT_ALLOWED, NO_IDENTITY
from app.services.gateway_resolver import GatewayResolver, ResolvedGateway
from app.services.proxy import Prepared
from app.services.request_log import RequestLogService, RequestRecorder, StreamRecorder
from app.services.retrieval import MemoryService, Recall
from app.services.routing import Attempts, Router, plan

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
) -> Response:
    """Forward a chat completion, streaming or not."""
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
    )

    attempts = Attempts(on_prepared=_record_prompt(recorder))
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
        recall = await _recall(memory, gateway, chat, request.headers, who=who, identity=identity)
        recorder.retrieval(latency_ms=recall.latency_ms if recall.attempted else None)
        # SPEC §6.3. Raised here rather than inside the retriever so the editor can render
        # the same failure as a diagnostic instead of a 503.
        recall.enforce(gateway.memory.on_retrieval_error)

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
                observer=StreamRecorder(recorder),
                recall=recall,
            )
            recorder.attempts(attempts.as_json())
            memory.injected(opened.prepared.memory_tokens)
            # Deliberately not submitted here: the observer owns the record from now on
            # and submits it when the stream ends, however it ends.
            return StreamingResponse(
                opened.stream.frames(),
                media_type="text/event-stream",
                headers={
                    **_headers(opened.prepared, recall),
                    "cache-control": "no-cache",
                    # Tells nginx not to buffer the response; without it an ingress can
                    # hold the whole stream and hand the client one lump at the end.
                    "x-accel-buffering": "no",
                },
            )

        completed = await router_.complete(chat, gateway, routing, attempts, recall=recall)
        recorder.attempts(attempts.as_json())
        memory.injected(completed.prepared.memory_tokens)
        recorder.from_response(completed.response)
        recorder.submit()
        return JSONResponse(
            content=completed.response.model_dump(exclude_none=True),
            headers=_headers(completed.prepared, recall),
        )
    except BaseException as error:
        # Every failure after authorization is somebody's, and the row is the only place
        # they will see it: the client gets an error body and this screen is where they
        # come to ask why. `BaseException` so a cancelled request is recorded too. The
        # attempts go on first: a chain that exhausted itself is the whole explanation,
        # and it lives in the object the raise passed straight through.
        recorder.attempts(attempts.as_json())
        recorder.failed(error)
        recorder.submit()
        raise


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


def _headers(prepared: Prepared, recall: Recall) -> dict[str, str]:
    """Which model answered, what memory added, and what this gateway refused to let the
    client change."""
    headers = {MODEL_HEADER: prepared.target.name}
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
        recorder.prepared(prepared.request.messages, prepared.target)
        if prepared.assembly is not None:
            recorder.injected(
                tokens=prepared.assembly.memory_tokens,
                chunks=prepared.assembly.chunk_log(),
                facts=prepared.assembly.fact_log(),
            )

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
