"""Who is asking, as far as this build can tell.

Task 12 gives end users rows, ids and memory. Until then routing needs *something*
stable to hash for A/B stickiness, and the honest answer is: whatever the caller told
us. This module is the whole of that answer, deliberately in one place with one export,
so task 12 replaces a module rather than hunting for header names.

Two sources, in this order.

``X-Gateway-User`` wins, because it is set by the customer's own backend — the thing that
knows which of *its* users this request belongs to — and it survives a client library
that strips unknown body fields.

``user`` on the request body is the OpenAI convention and the fallback, so an unmodified
OpenAI SDK call still buckets consistently.

The value is never stored. It is hashed for target selection and discarded; the
``end_user_id`` column stays null until task 12 has a table for it to reference. Writing
an unresolved string into a UUID column would be the kind of shortcut that a later
migration has to undo.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.schemas.openai import ChatRequest

#: The explicit header. Named after the gateway rather than the provider because it is
#: this system's convention, not OpenAI's.
END_USER_HEADER = "X-Gateway-User"

#: Long enough for a UUID, an email or an opaque provider id; short enough that a caller
#: cannot make the hash input arbitrarily large.
MAX_KEY_LENGTH = 200


def end_user_key(request: ChatRequest, headers: Mapping[str, str]) -> str | None:
    """A stable per-caller string, or ``None`` when the request is anonymous.

    ``None`` is a real answer and not a failure: without it A/B selection is random per
    request, which is what SPEC §8.1 asks for.
    """
    for candidate in (headers.get(END_USER_HEADER), request.user):
        if candidate:
            value = candidate.strip()[:MAX_KEY_LENGTH]
            if value:
                return value
    return None


__all__ = ["END_USER_HEADER", "MAX_KEY_LENGTH", "end_user_key"]
