"""HTTP plumbing shared by every dialect.

Auth styles, timeouts and endpoint URLs are not dialect questions. A provider that wants
``x-api-key`` wants it whether it speaks OpenAI or Anthropic, and an operator who put a
header in ``extra_headers`` expects it on the wire either way. Keeping these here is what
makes a new adapter a translation problem rather than a networking one — which is the
claim task 02's seam makes and task 16 is the first chance to test.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from app.adapters.base import UpstreamTarget

# Used when `auth_type` is api_key_header and the model does not name a header itself.
# Providers that want a different one set it through `extra_headers`, which is applied
# last and therefore wins.
DEFAULT_API_KEY_HEADER = "x-api-key"

# Establishing a TCP+TLS connection should never take as long as generating a completion,
# so the connect budget is fixed and short while the read budget is per-model. httpx
# applies the read timeout to each read, which for a streaming response makes it the
# time-to-first-byte budget and then the gap-between-chunks budget.
CONNECT_TIMEOUT_SECONDS = 10.0
WRITE_TIMEOUT_SECONDS = 10.0
POOL_TIMEOUT_SECONDS = 5.0


def timeouts(read_seconds: float) -> dict[str, float]:
    return {
        "connect": CONNECT_TIMEOUT_SECONDS,
        "read": read_seconds,
        "write": WRITE_TIMEOUT_SECONDS,
        "pool": POOL_TIMEOUT_SECONDS,
    }


def build_headers(
    target: UpstreamTarget,
    *,
    stream: bool,
    dialect_headers: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The outbound headers, in increasing order of precedence.

    ``dialect_headers`` is what a provider requires to parse the request at all —
    Anthropic's ``anthropic-version``, for instance. It sits below the auth header and
    below ``extra_headers`` so an operator pinning a different API version still can,
    which is the escape hatch that keeps a provider's breaking change from needing a
    release here.
    """
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }
    headers.update({name.lower(): value for name, value in (dialect_headers or {}).items()})
    headers.update(_auth_headers(target))
    # Explicit per-model headers are applied last so an operator can override anything
    # above, including the auth header name.
    headers.update({name.lower(): value for name, value in target.extra_headers.items()})
    return headers


def _auth_headers(target: UpstreamTarget) -> dict[str, str]:
    if target.auth_type == "none" or not target.credential:
        return {}
    if target.auth_type == "bearer":
        return {"authorization": f"Bearer {target.credential}"}
    if target.auth_type == "api_key_header":
        return {DEFAULT_API_KEY_HEADER: target.credential}
    if target.auth_type == "azure":
        # Azure OpenAI keys go in `api-key`; the deployment and api-version live in the
        # base URL, which is why the query string is preserved below.
        return {"api-key": target.credential}
    raise ValueError(f"unknown auth_type {target.auth_type!r}")


def endpoint_url(base_url: str, path: str) -> str:
    """Append an endpoint path while keeping any query string on the base URL.

    Azure OpenAI carries ``?api-version=`` on the base URL; naive concatenation would
    produce ``.../chat/completions?api-version=...`` only by accident, and dropping it
    yields a 404 that looks like a wrong deployment name.
    """
    parts = urlsplit(base_url)
    joined = parts.path.rstrip("/") + "/" + path.lstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, joined, parts.query, ""))
