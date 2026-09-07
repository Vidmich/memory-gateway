"""The OpenAI-compatible data plane: ``/g/{slug}/v1/*``.

The request body is validated here rather than by a FastAPI body parameter. That is
deliberate: FastAPI would raise ``RequestValidationError`` and produce its own envelope,
and a client SDK reading a non-OpenAI error body surfaces an unhelpful generic exception.
Parsing explicitly keeps every failure on this route in the shape callers can read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api.proxy.deps import (
    get_authenticator,
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
from app.services.end_user import end_user_key
from app.services.gateway_resolver import GatewayResolver, ResolvedGateway
from app.services.proxy import Prepared
from app.services.request_log import RequestLogService, RequestRecorder, StreamRecorder
from app.services.routing import Attempts, Router, plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/g/{slug}/v1", tags=["proxy"])

MODEL_HEADER = "X-Gateway-Model"
#: Names the locked parameters that replaced a value the client asked for. Present only
#: when something was actually overridden — a header on every response would be noise,
#: and the one case it matters is a caller wondering why `temperature` had no effect.
LOCKED_HEADER = "X-Gateway-Locked-Params"

Resolver = Annotated[GatewayResolver, Depends(get_resolver)]
Authenticator = Annotated[KeyAuthenticator, Depends(get_authenticator)]
Routing = Annotated[Router, Depends(get_router)]
Logs = Annotated[RequestLogService, Depends(get_request_logs)]


@router.post("/chat/completions")
async def chat_completions(
    slug: str,
    request: Request,
    resolver: Resolver,
    authenticator: Authenticator,
    router_: Routing,
    logs: Logs,
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

        # Which upstream, and what happens when it does not answer (SPEC §8.1). Resolved
        # before any time is spent so that a misconfigured gateway fails identically in
        # all three modes, and so the attempt list is fixed before the first call.
        routing = plan(gateway, end_user_key=end_user_key(chat, request.headers))

        recorder.upstream_call_started()
        if chat.stream:
            # Opening the stream sends the request and checks the status *before* any
            # bytes go downstream, so an upstream failure is still an HTTP error rather
            # than a truncated 200 — and, until this returns, it is still a failure the
            # next target can absorb (SPEC §8.2).
            opened = await router_.open_stream(
                chat, gateway, routing, attempts, observer=StreamRecorder(recorder)
            )
            recorder.attempts(attempts.as_json())
            # Deliberately not submitted here: the observer owns the record from now on
            # and submits it when the stream ends, however it ends.
            return StreamingResponse(
                opened.stream.frames(),
                media_type="text/event-stream",
                headers={
                    **_headers(opened.prepared),
                    "cache-control": "no-cache",
                    # Tells nginx not to buffer the response; without it an ingress can
                    # hold the whole stream and hand the client one lump at the end.
                    "x-accel-buffering": "no",
                },
            )

        completed = await router_.complete(chat, gateway, routing, attempts)
        recorder.attempts(attempts.as_json())
        recorder.from_response(completed.response)
        recorder.submit()
        return JSONResponse(
            content=completed.response.model_dump(exclude_none=True),
            headers=_headers(completed.prepared),
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


def _headers(prepared: Prepared) -> dict[str, str]:
    """Which model answered, and what this gateway refused to let the client change."""
    headers = {MODEL_HEADER: prepared.target.name}
    if prepared.params.overridden:
        # The gateway ignored something the client explicitly asked for. Saying so is the
        # difference between "this endpoint ignores temperature" as a bug report and as a
        # documented policy the caller can read off the response.
        headers[LOCKED_HEADER] = ",".join(prepared.params.overridden)
    return headers


def _record_prompt(recorder: RequestRecorder) -> Callable[[Prepared], None]:
    """Tell the log what each attempt actually sent.

    A closure rather than handing the recorder to the router: routing has no business
    importing the request log, and this is the only thing it would want from it. It also
    means the transcript follows the chain — two targets can carry different system
    contexts, and the prompt worth storing is the one the target that answered received.
    """

    def record(prepared: Prepared) -> None:
        recorder.prepared(prepared.request.messages, prepared.target)

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
