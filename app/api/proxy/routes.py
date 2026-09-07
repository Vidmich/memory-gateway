"""The OpenAI-compatible data plane: ``/g/{slug}/v1/*``.

The request body is validated here rather than by a FastAPI body parameter. That is
deliberate: FastAPI would raise ``RequestValidationError`` and produce its own envelope,
and a client SDK reading a non-OpenAI error body surfaces an unhelpful generic exception.
Parsing explicitly keeps every failure on this route in the shape callers can read.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api.proxy.deps import (
    get_authenticator,
    get_proxy_service,
    get_request_logs,
    get_resolver,
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
from app.services.gateway_resolver import GatewayResolver, ResolvedGateway
from app.services.proxy import ProxyService
from app.services.request_log import RequestLogService, StreamRecorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/g/{slug}/v1", tags=["proxy"])

MODEL_HEADER = "X-Gateway-Model"
#: Names the locked parameters that replaced a value the client asked for. Present only
#: when something was actually overridden — a header on every response would be noise,
#: and the one case it matters is a caller wondering why `temperature` had no effect.
LOCKED_HEADER = "X-Gateway-Locked-Params"

Resolver = Annotated[GatewayResolver, Depends(get_resolver)]
Authenticator = Annotated[KeyAuthenticator, Depends(get_authenticator)]
Proxy = Annotated[ProxyService, Depends(get_proxy_service)]
Logs = Annotated[RequestLogService, Depends(get_request_logs)]


@router.post("/chat/completions")
async def chat_completions(
    slug: str,
    request: Request,
    resolver: Resolver,
    authenticator: Authenticator,
    service: Proxy,
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

        target = gateway.target()
        prepared = service.prepare(chat, gateway, target)
        recorder.prepared(prepared.request.messages, target)

        headers = {MODEL_HEADER: target.name}
        if prepared.params.overridden:
            # The gateway ignored something the client explicitly asked for. Saying so is
            # the difference between "this endpoint ignores temperature" as a bug report
            # and as a documented policy the caller can read off the response.
            headers[LOCKED_HEADER] = ",".join(prepared.params.overridden)

        recorder.upstream_call_started()
        if chat.stream:
            # Opening the stream sends the request and checks the status *before* any
            # bytes go downstream, so an upstream failure is still an HTTP error rather
            # than a truncated 200.
            observer = StreamRecorder(recorder)
            stream = await service.open_stream(prepared, observer=observer)
            # Deliberately not submitted here: the observer owns the record from now on
            # and submits it when the stream ends, however it ends.
            return StreamingResponse(
                stream.frames(),
                media_type="text/event-stream",
                headers={
                    **headers,
                    "cache-control": "no-cache",
                    # Tells nginx not to buffer the response; without it an ingress can
                    # hold the whole stream and hand the client one lump at the end.
                    "x-accel-buffering": "no",
                },
            )

        completion = await service.complete(prepared)
        recorder.from_response(completion)
        recorder.submit()
        return JSONResponse(content=completion.model_dump(exclude_none=True), headers=headers)
    except BaseException as error:
        # Every failure after authorization is somebody's, and the row is the only place
        # they will see it: the client gets an error body and this screen is where they
        # come to ask why. `BaseException` so a cancelled request is recorded too.
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
