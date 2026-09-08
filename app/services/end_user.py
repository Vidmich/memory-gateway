"""Who is asking, and which conversation this is (SPEC §6.2).

Two questions, resolved per request, before anything is recalled.

**Identity** decides whose memory this is, and it is the only input to a feature that
stores durable personal facts — so its precedence is deliberate. ``X-Gateway-User`` wins
because it is set by the customer's own backend, the one component that actually knows
which of *its* users this request belongs to. The OpenAI ``user`` body field is second, so
an unmodified SDK call still identifies. The anonymous fallback is third and **off by
default**: ``anon:{sha256(api_key_id + client_ip)[:16]}`` is a coarse identity that merges
everyone behind one office NAT into a single person and splits one person across two
networks, and neither of those is a surprise anybody should get by accident from a feature
that persists what it learns. With it off, an unidentified caller simply gets no
conversation memory, which is the correct amount of memory to keep about somebody you
cannot name.

**Session** decides which conversation a turn belongs to. It is not used for isolation —
memory is scoped to ``(organization, end_user)``, never to a session — but task 13
debounces distillation by ``(end_user_id, session_id)``, so an id that changed every turn
would defeat the coalescing and distil each turn separately.

That is why this module **does not** implement SPEC §6.2's session fallback literally. The
SPEC says to hash "the serialized message list minus the final turn, so a growing
conversation hashes to a stable thread id across turns", and those two clauses do not
describe the same thing: turn 3 sends ``[u1, a1, u2, a2, u3]`` where turn 2 sent
``[u1, a1, u2]``, so dropping the last message leaves ``[u1, a1, u2, a2]`` against
``[u1, a1]`` — a different hash every turn, and therefore not a thread id at all. What
*is* byte-identical on every request of one thread is its **opening**: the messages up to
and including the first user turn. That is what is hashed here, salted with the end user,
so two people opening with "hi" are two threads. Two conversations by the same person
opening with exactly the same words are one thread as far as a fallback can tell, and
``X-Gateway-Session`` is how a client that cares says otherwise.

Everything resolved here is **untrusted input**. It arrives in a header or a body field,
which in a badly built integration means it arrives from a browser. So it is stripped of
control characters, capped in length, and never interpolated anywhere — not into a query,
which is parameterised, and above all not into a prompt: the memory block carries fact
*text*, never the identity that selected it.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.schemas.openai import ChatMessage, ChatRequest
from app.services.prompt import as_text

#: The explicit identity header. Named after the gateway rather than the provider because
#: it is this system's convention, not OpenAI's.
END_USER_HEADER = "X-Gateway-User"
#: The explicit thread header. Same reasoning.
SESSION_HEADER = "X-Gateway-Session"

#: Longest identity accepted, matching ``end_users.external_id``. Long enough for a UUID,
#: an email or an opaque provider subject; short enough that a caller cannot make a
#: unique-index key arbitrarily large.
MAX_KEY_LENGTH = 200

#: Longest session id accepted, matching ``request_logs.session_id``.
MAX_SESSION_LENGTH = 128

#: Where an identity came from. Recorded rather than inferred, because "this gateway
#: remembers nothing about anybody" has two completely different causes — nobody is
#: sending an id, or everybody is anonymous and anonymous memory is off — and they need
#: different fixes.
FROM_HEADER = "header"
FROM_BODY = "body"
ANONYMOUS = "anonymous"

#: SPEC §6.2's shape, verbatim. The prefix is part of the stored value on purpose: an
#: operator reading the end-users table can see at a glance which rows are a real identity
#: and which are an IP-derived guess, and a purge of the guesses is a prefix match.
ANON_PREFIX = "anon:"
ANON_DIGEST_CHARS = 16

#: C0 and C1 controls, minus nothing: none of them belongs in an identifier, and a
#: newline is the one that turns a log line or a table cell into two.
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class EndUserIdentity:
    """A resolved end user, and how we came to believe it.

    ``None`` from :func:`resolve_identity` is a real answer — the caller sent no identity
    and this gateway does not invent one — so the absence of this object is what switches
    conversation memory off for a request rather than an empty string standing in for it.
    """

    external_id: str
    source: str

    @property
    def anonymous(self) -> bool:
        return self.source == ANONYMOUS


def resolve_identity(
    request: ChatRequest,
    headers: Mapping[str, str],
    *,
    api_key_id: uuid.UUID | None = None,
    client_ip: str | None = None,
    allow_anonymous: bool = False,
) -> EndUserIdentity | None:
    """SPEC §6.2's three sources, first match wins.

    The anonymous branch needs both an API key and a client address. Missing either, it
    returns ``None`` rather than hashing a partial input: an identity derived from one
    known value and one empty string is the *same* identity for every caller behind that
    key, which would pool strangers' facts into one profile — the exact failure the
    default-off setting exists to prevent, arriving by a different route.
    """
    explicit = (
        (headers.get(END_USER_HEADER), FROM_HEADER),
        (request.user, FROM_BODY),
    )
    for candidate, source in explicit:
        if cleaned := clean_key(candidate):
            return EndUserIdentity(external_id=cleaned, source=source)

    if not allow_anonymous or api_key_id is None or not client_ip:
        return None
    return EndUserIdentity(external_id=anonymous_id(api_key_id, client_ip), source=ANONYMOUS)


def anonymous_id(api_key_id: uuid.UUID, client_ip: str) -> str:
    """``anon:{sha256(api_key_id + client_ip)[:16]}``.

    Truncated to sixteen hex characters — sixty-four bits — because this is a bucket
    label, not a secret. Two different callers colliding would merge their memories, and
    at sixty-four bits that needs on the order of four billion distinct addresses behind
    one key before it becomes likely.
    """
    digest = hashlib.sha256(f"{api_key_id}{client_ip}".encode()).hexdigest()
    return f"{ANON_PREFIX}{digest[:ANON_DIGEST_CHARS]}"


def session_key(
    messages: Sequence[ChatMessage],
    headers: Mapping[str, str],
    *,
    external_id: str | None = None,
) -> str | None:
    """Which conversation this turn belongs to, or ``None`` when there is nothing to hash.

    See the module docstring for why the fallback hashes the conversation's *opening*
    rather than SPEC §6.2's "message list minus the final turn".
    """
    if explicit := clean_key(headers.get(SESSION_HEADER), limit=MAX_SESSION_LENGTH):
        return explicit

    opening = _opening(messages)
    if not opening:
        # No user turn at all — a bare system message, or an assistant prefill. There is
        # no conversation to name yet, and inventing one would group every such request
        # in the organization under a single id.
        return None

    # The end user is part of the input so that two people whose threads open with the
    # same words are two threads. Without it, "hi" would be one session shared by an
    # entire customer's user base.
    material = "\n".join([external_id or "", *opening])
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _opening(messages: Sequence[ChatMessage]) -> list[str]:
    """Every message up to and including the first user turn, rendered.

    The longest prefix of a conversation that is guaranteed to arrive unchanged on every
    subsequent request of the same thread — clients resend history, they do not rewrite
    it. Assistant turns cannot appear before the first user turn in any real
    conversation, so this is in practice the system messages plus the opening question.
    """
    rendered: list[str] = []
    for message in messages:
        rendered.append(f"{message.role}:{as_text(message.content).strip()}")
        if message.role == "user":
            return rendered
    return []


def clean_key(value: str | None, *, limit: int = MAX_KEY_LENGTH) -> str | None:
    """Strip, de-control and truncate a caller-supplied identifier.

    Control characters are removed rather than rejected. A 400 would be the purist's
    answer, but the value arrives as an *optional* field on a request whose real purpose
    is a completion — refusing the whole request because somebody's user id has a stray
    tab in it turns a cosmetic problem into an outage for that integration. What is left
    after stripping is still a stable key for the same caller.

    Truncation is applied last so the length bound holds on what is stored, not on what
    was sent.
    """
    if not value:
        return None
    cleaned = _CONTROLS.sub("", value).strip()[:limit].strip()
    return cleaned or None


def end_user_key(request: ChatRequest, headers: Mapping[str, str]) -> str | None:
    """The A/B stickiness key (SPEC §8.1).

    The same two explicit sources as :func:`resolve_identity`, and deliberately *not* the
    anonymous fallback: an IP-derived id changes when somebody moves between networks, so
    bucketing on it would move that person between A and B mid-experiment — which is the
    one thing sticky routing exists to prevent. A caller with no identity is assigned per
    request, which is what SPEC §8.1 asks for.
    """
    identity = resolve_identity(request, headers, allow_anonymous=False)
    return identity.external_id if identity is not None else None


__all__ = [
    "ANONYMOUS",
    "ANON_DIGEST_CHARS",
    "ANON_PREFIX",
    "END_USER_HEADER",
    "FROM_BODY",
    "FROM_HEADER",
    "MAX_KEY_LENGTH",
    "MAX_SESSION_LENGTH",
    "SESSION_HEADER",
    "EndUserIdentity",
    "anonymous_id",
    "clean_key",
    "end_user_key",
    "resolve_identity",
    "session_key",
]
