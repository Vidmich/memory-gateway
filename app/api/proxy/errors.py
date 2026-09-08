"""Data-plane errors.

Every one of these renders as the OpenAI error envelope — the branch lives in
``app.core.errors.error_response``, keyed on the ``/g/`` path prefix, so Starlette's own
405s and unhandled exceptions come out in the same shape.

Status codes are chosen for what the caller should *do*: 504 for "the upstream took too
long" and 502 for "the upstream was unreachable" are retryable in a way that a 500 from
the gateway is not.
"""

from __future__ import annotations

from app.core.errors import AppError


class ProxyError(AppError):
    """Base for failures on the data plane."""

    openai_type: str | None = "invalid_request_error"


class InvalidRequest(ProxyError):
    status_code = 400
    code = "invalid_request"


class UnsupportedField(InvalidRequest):
    """A field the gateway cannot honour and refuses to drop silently (SPEC §12.1)."""

    code = "unsupported_field"

    def __init__(self, field: str) -> None:
        super().__init__(
            f"This gateway does not support the '{field}' field. It is not forwarded to the "
            f"upstream model, so the request is refused rather than silently answered "
            f"without it.",
            param=field,
        )


class AuthenticationFailed(ProxyError):
    status_code = 401
    code = "invalid_api_key"


class PermissionDenied(ProxyError):
    status_code = 403
    code = "key_not_for_gateway"


class GatewayNotFound(ProxyError):
    status_code = 404
    code = "gateway_not_found"


class ModelNotFound(ProxyError):
    status_code = 404
    code = "model_not_found"

    def __init__(self, message: str) -> None:
        super().__init__(message, param="model")


class GatewayDisabled(ProxyError):
    """Switched off by its owner.

    403 rather than 503, and the difference matters to a client. A 503 means "try again",
    and an SDK will — every few seconds, indefinitely, against an endpoint somebody turned
    off on purpose. A 403 is final, so the retry loop stops and the message is what gets
    read.
    """

    status_code = 403
    code = "gateway_disabled"


class GatewayUnavailable(ProxyError):
    """The gateway exists and is enabled, but has no usable target.

    Retryable on purpose: the fix is a toggle in the UI, so a client backing off and
    trying again is the behaviour that recovers on its own.
    """

    status_code = 503
    code = "gateway_unavailable"
    openai_type = "server_error"


class RateLimitUnavailable(ProxyError):
    """The limiter could not reach its counters and this deployment fails closed.

    503 rather than 429, because the client did nothing wrong: nothing has been counted,
    no budget has been exceeded, and the gateway is declining to serve traffic it cannot
    account for. A 429 would tell the caller to slow down, which is not the fix and would
    put the blame on the wrong side of the connection. ``Retry-After`` is short because a
    Redis blip is measured in seconds.
    """

    status_code = 503
    code = "rate_limit_unavailable"
    openai_type = "server_error"

    def __init__(self, message: str, *, retry_after_seconds: int = 5) -> None:
        super().__init__(message, headers={"retry-after": str(max(1, retry_after_seconds))})


class UpstreamTimeout(ProxyError):
    status_code = 504
    code = "upstream_timeout"
    openai_type = "server_error"


class UpstreamUnavailable(ProxyError):
    status_code = 502
    code = "upstream_unavailable"
    openai_type = "server_error"


class UpstreamStatus(ProxyError):
    """A 4xx or 5xx relayed from the provider.

    The provider's own status, message, type and code are preserved so a client SDK
    raises what it would have raised talking to the provider directly — a 429 stays a
    ``RateLimitError``. The message is tagged with the model name so it is unambiguous
    that the gateway is reporting, not producing, the failure.
    """

    code = "upstream_error"

    def __init__(
        self,
        *,
        status_code: int,
        model_name: str,
        message: str,
        upstream_type: str | None = None,
        upstream_code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(f"[upstream:{model_name}] {message}", param=param)
        self.status_code = status_code
        self.openai_type = upstream_type or None
        if upstream_code:
            self.code = upstream_code
