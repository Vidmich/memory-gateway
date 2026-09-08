"""The outbound request guard (task 18).

The thing being tested is not "does it block 127.0.0.1". That is one line. It is the two
harder properties:

* **the check and the connection agree** — the address that was validated is the address
  that gets dialled, so a name whose second lookup answers differently cannot be used to
  slip past a check made on the first one;
* **the guarded pool is the one tenant URLs go through**, and the operator's own endpoints
  go through a different one, so hardening the first does not break the second.
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.core.ssrf import (
    BlockedAddress,
    GuardedTransport,
    UrlPolicy,
    build_policy,
    check_url,
    classify,
)


class ScriptedResolver:
    """Answers a hostname from a list, one answer per lookup.

    The rebinding tests need the *second* lookup to differ from the first, which no real
    resolver can be asked to do on demand.
    """

    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        if not self.answers:
            raise OSError(f"no answer scripted for {host}")
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class RecordingTransport(httpx.AsyncBaseTransport):
    """The transport the guard wraps. Records what it was actually asked to dial."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)


def guarded(
    policy: UrlPolicy, *answers: list[str]
) -> tuple[httpx.AsyncClient, RecordingTransport, ScriptedResolver]:
    inner = RecordingTransport()
    resolver = ScriptedResolver(*answers)
    client = httpx.AsyncClient(
        transport=GuardedTransport(inner, policy=policy, resolver=resolver),
        follow_redirects=False,
    )
    return client, inner, resolver


BLOCKING = build_policy(allow_private=False)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("0.0.0.0", "unspecified"),
        ("10.1.2.3", "private"),
        ("172.16.0.5", "private"),
        ("192.168.1.1", "private"),
        # The one this module mostly exists for.
        ("169.254.169.254", "link-local"),
        ("224.0.0.1", "multicast"),
        ("::1", "loopback"),
        ("fe80::1", "link-local"),
        ("fd00::1", "private"),
        # Carrier-grade NAT. On a blocklist this is the range somebody forgot; the rule
        # here is "globally routable or not", so it needs no maintenance to cover it.
        ("100.64.0.1", "non-routable"),
    ],
)
def test_addresses_inside_a_network_are_refused(address: str, reason: str) -> None:
    assert classify(ipaddress.ip_address(address)) == reason


@pytest.mark.parametrize("address", ["1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert classify(ipaddress.ip_address(address)) is None


def test_an_ipv4_loopback_wearing_an_ipv6_costume_is_still_loopback() -> None:
    # `IPv6Address("::ffff:127.0.0.1").is_loopback` is False, which is the whole reason
    # `_unwrap` exists: the question is which machine the packet reaches.
    assert classify(ipaddress.ip_address("::ffff:127.0.0.1")) == "loopback"
    assert classify(ipaddress.ip_address("::ffff:169.254.169.254")) == "link-local"


def test_a_6to4_address_is_unwrapped_too() -> None:
    assert classify(ipaddress.ip_address("2002:7f00:0001::")) == "loopback"


# ---------------------------------------------------------------------------
# the write-time check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1",
        "https://10.0.0.1/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:11434/v1",
        "http://vault.internal/v1",
        "http://printer.local/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_a_url_pointing_inside_the_network_is_refused_on_save(url: str) -> None:
    with pytest.raises(ValueError):
        check_url(url, BLOCKING)


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://1.1.1.1/v1"])
def test_a_public_url_is_accepted_on_save(url: str) -> None:
    check_url(url, BLOCKING)


def test_a_hostname_is_not_resolved_at_save_time() -> None:
    """A validator that makes a DNS query is a save that fails when a resolver is slow.

    The name is accepted here and decided at connect time, where the answer is fresh.
    """
    check_url("https://whatever.example.com/v1", BLOCKING)


@pytest.mark.parametrize("scheme", ["ftp://host/v1", "file:///etc/passwd", "gopher://host/"])
def test_only_http_urls_are_accepted(scheme: str) -> None:
    with pytest.raises(ValueError, match="http"):
        check_url(scheme, BLOCKING)


def test_an_allowlisted_host_passes_the_write_time_check() -> None:
    policy = build_policy(allow_private=False, allowed_hosts=["vllm.internal"])

    check_url("http://vllm.internal:8000/v1", policy)
    with pytest.raises(ValueError):
        check_url("http://other.internal:8000/v1", policy)


def test_nothing_is_refused_when_the_guard_is_off() -> None:
    check_url("http://localhost:11434/v1", build_policy(allow_private=True))


# ---------------------------------------------------------------------------
# the transport
# ---------------------------------------------------------------------------


async def test_a_public_name_is_dialled_at_the_address_that_was_validated() -> None:
    client, inner, resolver = guarded(BLOCKING, ["93.184.216.34"])

    async with client:
        await client.get("https://example.com/v1/models")

    sent = inner.requests[0]
    # The URL now names the IP, not the host. That is the pin: httpx will not resolve
    # anything, so there is no second lookup to poison.
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "example.com"
    # And the certificate is still verified against the name the operator configured.
    assert sent.extensions["sni_hostname"] == "example.com"
    assert resolver.calls == [("example.com", 443)]


async def test_a_name_that_resolves_inside_the_network_is_refused() -> None:
    client, inner, _ = guarded(BLOCKING, ["10.0.0.7"])

    async with client:
        with pytest.raises(BlockedAddress) as caught:
            await client.get("https://internal.example.com/v1")

    assert caught.value.reason == "private"
    assert caught.value.host == "internal.example.com"
    assert inner.requests == [], "nothing should have been dialled"


async def test_dns_rebinding_is_refused_because_every_answer_is_checked() -> None:
    """The attack this module is shaped around.

    A name that answers with a public address *and* a loopback one is not a name with one
    good answer; which one arrives first is the attacker's choice. So all of them have to
    pass, not just the one that would be dialled.
    """
    client, inner, _ = guarded(BLOCKING, ["93.184.216.34", "127.0.0.1"])

    async with client:
        with pytest.raises(BlockedAddress) as caught:
            await client.get("https://rebind.example.com/v1")

    assert caught.value.reason == "loopback"
    assert inner.requests == []


async def test_a_second_lookup_cannot_change_the_answer_because_there_is_no_second_lookup() -> None:
    """The time-of-check/time-of-use half of rebinding.

    A resolver that answers publicly the first time and privately the second would defeat
    any design that validates a name and then hands the *name* to the socket layer. Here
    the second answer is never asked for: the request carries the address that passed.
    """
    client, inner, resolver = guarded(BLOCKING, ["93.184.216.34"], ["127.0.0.1"])

    async with client:
        await client.get("https://rebind.example.com/v1")

    assert inner.requests[0].url.host == "93.184.216.34"
    assert len(resolver.calls) == 1


async def test_a_literal_public_address_is_passed_through_untouched() -> None:
    client, inner, resolver = guarded(BLOCKING)

    async with client:
        await client.get("https://1.1.1.1/v1")

    assert inner.requests[0].url.host == "1.1.1.1"
    # No rewrite and no lookup: there is nothing to resolve and nothing to pin.
    assert "sni_hostname" not in inner.requests[0].extensions
    assert resolver.calls == []


async def test_a_literal_private_address_is_refused() -> None:
    client, inner, _ = guarded(BLOCKING)

    async with client:
        with pytest.raises(BlockedAddress):
            await client.get("http://169.254.169.254/latest/meta-data/iam/")

    assert inner.requests == []


async def test_an_allowlisted_host_is_dialled_by_name() -> None:
    """Not pinned, deliberately: the operator has said this name is fine, and pinning it
    would break a service whose pods move — which is what a cluster DNS name is for."""
    policy = build_policy(allow_private=False, allowed_hosts=["vllm.models.svc.cluster.local"])
    client, inner, resolver = guarded(policy, ["10.42.0.9"])

    async with client:
        await client.get("http://vllm.models.svc.cluster.local:8000/v1/models")

    assert inner.requests[0].url.host == "vllm.models.svc.cluster.local"
    assert resolver.calls == []


async def test_an_allowlisted_cidr_permits_whatever_name_resolves_into_it() -> None:
    policy = build_policy(allow_private=False, allowed_cidrs=["10.42.0.0/16"])
    client, inner, _ = guarded(policy, ["10.42.0.9"])

    async with client:
        await client.get("http://models.svc.cluster.local:8000/v1")

    assert inner.requests[0].url.host == "10.42.0.9"


async def test_an_address_outside_the_allowlisted_cidr_is_still_refused() -> None:
    policy = build_policy(allow_private=False, allowed_cidrs=["10.42.0.0/16"])
    client, _, _ = guarded(policy, ["10.99.0.1"])

    async with client:
        with pytest.raises(BlockedAddress):
            await client.get("http://elsewhere.svc.cluster.local:8000/v1")


async def test_a_resolution_failure_is_a_connect_error_not_a_block() -> None:
    """A name that does not resolve is a typo, not an attack, and the message has to say
    so — otherwise every misspelled host reads as a security refusal."""
    client, _, _ = guarded(BLOCKING)

    async with client:
        with pytest.raises(httpx.ConnectError) as caught:
            await client.get("https://nonexistent.example.com/v1")

    assert not isinstance(caught.value, BlockedAddress)
    assert "resolve" in str(caught.value)


async def test_the_guard_does_nothing_when_private_addresses_are_allowed() -> None:
    client, inner, resolver = guarded(build_policy(allow_private=True), ["127.0.0.1"])

    async with client:
        await client.get("http://localhost:11434/v1/models")

    assert inner.requests[0].url.host == "localhost"
    assert resolver.calls == []


def test_a_blocked_address_reads_as_a_transport_failure() -> None:
    """It subclasses ``httpx.ConnectError`` so every existing caller handles it: the proxy
    turns it into a 502 naming the model, the probe reports it under the button."""
    error = BlockedAddress("evil.example.com", "127.0.0.1", "loopback")

    assert isinstance(error, httpx.ConnectError)
    assert "evil.example.com" in str(error)
    assert "loopback" in str(error)


# ---------------------------------------------------------------------------
# the policy the deployment actually gets
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


def test_production_blocks_private_addresses_by_default() -> None:
    policy = _settings(environment="prod", upstream_private_addresses="auto").upstream_url_policy

    assert policy.enforced


def test_development_allows_them_by_default() -> None:
    """Somebody pointing a model at ``http://localhost:11434`` is running Ollama."""
    policy = _settings(environment="dev", upstream_private_addresses="auto").upstream_url_policy

    assert not policy.enforced


def test_block_and_allow_ignore_the_environment() -> None:
    assert _settings(
        environment="dev", upstream_private_addresses="block"
    ).upstream_url_policy.enforced
    assert not _settings(
        environment="prod", upstream_private_addresses="allow"
    ).upstream_url_policy.enforced


def test_the_allowlists_reach_the_policy() -> None:
    policy = _settings(
        environment="prod",
        upstream_allowed_hosts=("VLLM.Internal.",),
        upstream_allowed_cidrs=("10.42.0.0/16",),
    ).upstream_url_policy

    # Normalised, so a trailing dot or a capital in configuration is not a silent miss.
    assert policy.permits_host("vllm.internal")
    assert policy.permits(ipaddress.ip_address("10.42.1.1"))
    assert not policy.permits(ipaddress.ip_address("10.99.1.1"))
