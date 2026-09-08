"""Refusing to fetch a URL that points back inside the network.

An organization user can set an upstream model's ``base_url`` to anything. That makes the
gateway an authenticated request forwarder with a position inside your VPC: a base URL of
``http://169.254.169.254/latest/meta-data/iam/`` turns "test connection" into a credential
read, and one of ``http://postgres.internal:5432`` turns it into a port scanner that
reports back through the error message. The product's whole feature is fetching a URL
somebody else chose, so this is not a hypothetical.

Two checks, deliberately not one.

**Statically**, when a model is saved: the scheme has to be http(s), and a literal IP has
to be globally routable. This exists for the person typing, not for the attacker — it puts
a red message under the field instead of a probe that fails a second later for a reason
nobody can read.

**At connect time**, in the transport, for every request: the hostname is resolved here,
*every* address it answers with is validated, and the connection is then pinned to one of
them. That ordering is the whole point. Validating a name and then handing the name to the
socket layer is a check of one DNS answer and a connection made on a second one — the
rebinding attack, where the first lookup returns a public address and the second returns
``127.0.0.1``. Because the address that was checked is the address that is dialled, there
is no second lookup to poison.

Pinning costs one thing worth naming: the connection is opened to an IP, so the TLS
handshake needs the original hostname put back for SNI and for certificate verification.
That is ``sni_hostname``, set below, and it is why the guard does not silently disable
certificate checking the way a naive rewrite would.

What is *not* guarded is the operator's own endpoints — Qdrant, MinIO, the embedding
provider. Those legitimately live on private addresses, they come from the environment
rather than from a tenant, and they go through a separate client
(:func:`app.core.clients.create_internal_client`) that has no guard on it. The boundary is
"did a tenant choose this URL", not "is this address private".
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Names that are always refused regardless of what they resolve to. Resolution already
#: catches these — ``localhost`` answers ``127.0.0.1`` — but a name is what somebody pastes
#: and a name is what the error should talk about.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        # AWS/GCP/Azure agree on the address; only Google publishes a name for it, and it
        # is the one that appears in every metadata-service write-up.
        "metadata.google.internal",
        "metadata.goog",
    }
)

#: Suffixes refused by name for the same reason. ``.internal`` and ``.local`` are not
#: resolvable from outside a network by construction, so a model pointing at one is either
#: a mistake or an attempt.
BLOCKED_SUFFIXES = ("localhost", ".localhost", ".internal", ".local", ".home.arpa")


class BlockedAddress(httpx.ConnectError):
    """A request was refused before a socket was opened.

    An ``httpx.ConnectError`` on purpose. Every caller of the shared client already
    handles transport failures — the proxy turns one into a 502 with the model's name, the
    probe reports the message verbatim under the "Test connection" button — and a new
    exception type would mean each of those growing a branch to reach the same place. What
    it adds is the *reason*, which is what makes the difference between a red box saying
    "could not be reached" and one saying the URL points at a link-local address.
    """

    def __init__(self, host: str, address: str | None, reason: str) -> None:
        where = f"{host} ({address})" if address and address != host else host
        super().__init__(f"refusing to connect to {where}: {reason} addresses are not allowed")
        self.host = host
        self.address = address
        self.reason = reason


def classify(address: IpAddress) -> str | None:
    """Why this address is not allowed, or ``None`` if it is.

    The rule is ``is_global`` — allow only what is routable on the public internet —
    rather than a list of blocked ranges. A blocklist is a list somebody has to keep
    current: it was missing ``100.64.0.0/10`` before carrier-grade NAT existed and will be
    missing whatever comes next. The named cases below only exist to produce a message
    that says which kind of address it was.
    """
    address = _unwrap(address)
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        # 169.254.169.254 — the cloud metadata service — lands here, which is the single
        # most valuable target this whole module exists to protect.
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if address.is_private:
        return "private"
    if not address.is_global:
        # Shared address space, benchmarking ranges, and whatever the IANA registry grows
        # next. Reached only when none of the friendlier names above applied.
        return "non-routable"
    return None


def _unwrap(address: IpAddress) -> IpAddress:
    """Follow an IPv6 address that is really an IPv4 one in disguise.

    ``::ffff:127.0.0.1`` is loopback, and ``IPv6Address.is_loopback`` says ``False`` about
    it. Both mappings Python models — v4-mapped and 6to4 — are unwrapped, because the
    question is which machine the packet reaches, not how the address was spelled.
    """
    if isinstance(address, ipaddress.IPv6Address):
        for mapped in (address.ipv4_mapped, address.sixtofour):
            if mapped is not None:
                return mapped
    return address


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """What this deployment lets a tenant-supplied URL point at.

    ``allow_private`` is the development posture, where a model pointing at
    ``http://localhost:11434`` is somebody running Ollama rather than an attack. It
    defaults to *on* outside production and is refused in production unless an operator
    sets it deliberately — the same shape as the ``EMBEDDING_PROVIDER=hash`` guard, and
    for the same reason: the failure is silent and the default is the whole protection.

    The two allowlists are for the legitimate internal endpoint — a self-hosted vLLM on the
    cluster network. They are environment configuration rather than a platform setting on a
    screen: this is the boundary of the network the process sits in, and moving it should
    take a deploy rather than a session.
    """

    allow_private: bool = False
    #: Exact hostnames, lowercased. Matching one skips address validation for that host.
    allowed_hosts: frozenset[str] = frozenset()
    #: Addresses inside one of these are permitted whatever name asked for them.
    allowed_networks: tuple[IpNetwork, ...] = ()

    @property
    def enforced(self) -> bool:
        return not self.allow_private

    def permits_host(self, host: str) -> bool:
        return host.lower().rstrip(".") in self.allowed_hosts

    def permits(self, address: IpAddress) -> bool:
        if self.allow_private:
            return True
        if classify(address) is None:
            return True
        return any(address in network for network in self.allowed_networks)


class Resolver(Protocol):
    """Hostname to addresses. A seam so the rebinding tests can answer differently on the
    first lookup and the second without a DNS server."""

    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    def __init__(self, family: int = socket.AF_UNSPEC) -> None:
        self._family = family

    async def resolve(self, host: str, port: int) -> list[str]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, family=self._family, type=socket.SOCK_STREAM)
        # Order-preserving dedupe: getaddrinfo returns one entry per (family, socktype,
        # protocol) and the same address therefore appears several times.
        seen: dict[str, None] = {}
        for info in infos:
            seen.setdefault(str(info[4][0]), None)
        return list(seen)


def check_url(url: str, policy: UrlPolicy) -> None:
    """The write-time check: raise :class:`ValueError` if this URL is refused outright.

    Only what can be decided without a network call. A hostname is *not* resolved here —
    a validator that makes a DNS query is a save that fails when a resolver is slow, and
    the answer would be stale by the time the request is made anyway. The transport is
    where the real decision is taken; this is the one that puts a message on the screen.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("must be an http:// or https:// URL")
    host = (parts.hostname or "").strip()
    if not host:
        raise ValueError("must include a host")
    if not policy.enforced:
        return
    if policy.permits_host(host):
        return
    lowered = host.lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_SUFFIXES):
        raise ValueError(f"must not point at {host}, which is inside this network")
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return  # a name; the transport decides once it knows what it resolves to
    if not policy.permits(address):
        raise ValueError(
            f"must not point at {host}, which is a {classify(address)} address inside this network"
        )


class GuardedTransport(httpx.AsyncBaseTransport):
    """Resolve, validate every answer, then dial the address that was validated.

    Wrapping a transport rather than checking in each caller is what makes this hold for
    code nobody has written yet. There is one shared client for tenant-supplied URLs; a
    future feature that sends a request through it is guarded by construction, and the way
    to opt out is to reach for the other client explicitly.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport,
        *,
        policy: UrlPolicy,
        resolver: Resolver | None = None,
    ) -> None:
        self._transport = transport
        self._policy = policy
        self._resolver = resolver or SystemResolver()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._policy.enforced:
            request = await self._pin(request)
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def _pin(self, request: httpx.Request) -> httpx.Request:
        host = request.url.host
        if not host or self._policy.permits_host(host):
            return request

        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            # Already an address: there is nothing to resolve and nothing to pin, so the
            # request goes through untouched rather than being rewritten into itself.
            self._require(host, [str(literal)])
            return request

        try:
            addresses = await self._resolver.resolve(host, port)
        except OSError as exc:
            raise httpx.ConnectError(f"could not resolve {host}", request=request) from exc
        if not addresses:
            raise httpx.ConnectError(f"could not resolve {host}", request=request)

        self._require(host, addresses)
        # Pinned to the first answer. The others were validated too, so failing over to
        # one of them would also be safe — but httpx dials what the URL says, and a URL
        # that still carries the name is a second lookup this whole class exists to avoid.
        return _rewritten(request, addresses[0], host)

    def _require(self, host: str, addresses: Sequence[str]) -> None:
        """Every answer has to be allowed, not just the one that will be dialled.

        A name that resolves to a public address *and* ``127.0.0.1`` is not a name with one
        good answer; it is the rebinding setup, and which one arrives first is the
        attacker's choice rather than ours.
        """
        for candidate in addresses:
            address = ipaddress.ip_address(candidate)
            if self._policy.permits(address):
                continue
            reason = classify(address) or "non-routable"
            logger.warning(
                "refused an upstream request to a blocked address",
                extra={"upstream_host": host, "blocked_reason": reason},
            )
            raise BlockedAddress(host, candidate, reason)


def _rewritten(request: httpx.Request, address: str, host: str) -> httpx.Request:
    """The same request, addressed to a validated IP but still speaking to ``host``.

    ``Host`` keeps the name so virtual hosting and path routing upstream still work, and
    ``sni_hostname`` keeps it for the TLS handshake so the certificate is verified against
    the name the operator configured rather than against an IP that has no certificate.
    """
    request.url = request.url.copy_with(host=address)
    port = request.url.port
    request.headers["host"] = f"{host}:{port}" if port else host
    request.extensions = {**request.extensions, "sni_hostname": host}
    return request


def build_policy(
    *,
    allow_private: bool,
    allowed_hosts: Sequence[str] = (),
    allowed_cidrs: Sequence[str] = (),
) -> UrlPolicy:
    return UrlPolicy(
        allow_private=allow_private,
        allowed_hosts=frozenset(host.lower().rstrip(".") for host in allowed_hosts if host),
        allowed_networks=tuple(ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs),
    )


__all__ = [
    "BLOCKED_HOSTNAMES",
    "BLOCKED_SUFFIXES",
    "BlockedAddress",
    "GuardedTransport",
    "Resolver",
    "SystemResolver",
    "UrlPolicy",
    "build_policy",
    "check_url",
    "classify",
]
