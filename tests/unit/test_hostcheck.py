from __future__ import annotations

import socket

import pytest

from url_crawler_service.hostcheck import Resolver, private_host_reason

PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"

PRIVATE_ADDRESSES = [
    "127.0.0.1",
    "10.0.0.5",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "224.0.0.1",
    "240.0.0.1",
    "0.0.0.0",
    "::1",
    "fd00::1",
    "fe80::1",
    "::",
]

LOCAL_HOSTNAMES = [
    "http://localhost/",
    "http://localhost:5432/",
    "http://db.localhost/",
    "http://printer.local/",
    "http://api.internal/",
    "http://LOCALHOST/",
]


def resolver(*addresses: str) -> Resolver:
    async def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


async def unresolvable(host: str) -> list[str]:
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


async def never_called(host: str) -> list[str]:
    raise AssertionError(f"{host} should not have been resolved")


async def test_a_host_resolving_to_a_public_address_is_allowed() -> None:
    assert await private_host_reason("http://example.com/", resolve=resolver(PUBLIC_IPV4)) is None


async def test_a_public_ipv6_host_is_allowed() -> None:
    assert await private_host_reason("http://example.com/", resolve=resolver(PUBLIC_IPV6)) is None


@pytest.mark.parametrize("address", PRIVATE_ADDRESSES)
async def test_a_host_resolving_to_a_private_address_is_rejected(address: str) -> None:
    reason = await private_host_reason("http://example.com/", resolve=resolver(address))

    assert reason == f"host resolves to a private address ({address})"


async def test_a_host_with_one_private_address_among_several_is_rejected() -> None:
    reason = await private_host_reason(
        "http://example.com/", resolve=resolver(PUBLIC_IPV4, "10.0.0.5")
    )

    assert reason == "host resolves to a private address (10.0.0.5)"


@pytest.mark.parametrize("url", LOCAL_HOSTNAMES)
async def test_a_local_hostname_is_rejected_without_resolving_it(url: str) -> None:
    reason = await private_host_reason(url, resolve=never_called)

    assert reason is not None
    assert "local hostname" in reason


async def test_a_trailing_dot_does_not_hide_a_local_hostname() -> None:
    assert await private_host_reason("http://localhost./", resolve=never_called) is not None


@pytest.mark.parametrize("address", PRIVATE_ADDRESSES)
async def test_a_private_ip_literal_is_rejected_without_resolving_it(address: str) -> None:
    host = f"[{address}]" if ":" in address else address
    reason = await private_host_reason(f"http://{host}/", resolve=never_called)

    assert reason == f"{address} is a private address"


async def test_a_public_ip_literal_is_allowed() -> None:
    assert await private_host_reason(f"http://{PUBLIC_IPV4}/", resolve=never_called) is None


async def test_a_host_that_does_not_resolve_is_rejected() -> None:
    assert await private_host_reason("http://nope.test/", resolve=unresolvable) == (
        "host does not resolve"
    )


async def test_a_host_that_resolves_to_nothing_is_rejected() -> None:
    assert await private_host_reason("http://nope.test/", resolve=resolver()) == (
        "host does not resolve"
    )


async def test_a_url_without_a_host_is_rejected() -> None:
    assert await private_host_reason("http:///pages", resolve=never_called) is not None
