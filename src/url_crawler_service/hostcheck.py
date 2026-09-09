"""Keeps the service from crawling itself: seeds that point at private addresses."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

Resolver = Callable[[str], Awaitable[list[str]]]

LOCAL_SUFFIXES = (".localhost", ".local", ".internal")
UNRESOLVABLE = "host does not resolve"


async def resolve_host(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]).partition("%")[0] for info in infos]


async def private_host_reason(url: str, *, resolve: Resolver = resolve_host) -> str | None:
    """Say why a seed must not be crawled from the service, or None when it is safe."""
    host = (urlsplit(url).hostname or "").rstrip(".")
    if not host:
        return "the URL has no host"
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return f"{host} is a local hostname"

    literal = _parse(host)
    if literal is not None:
        return f"{host} is a private address" if _is_private(literal) else None

    try:
        addresses = await resolve(host)
    except OSError:
        return UNRESOLVABLE
    if not addresses:
        return UNRESOLVABLE
    for address in addresses:
        parsed = _parse(address)
        if parsed is None or _is_private(parsed):
            return f"host resolves to a private address ({address})"
    return None


def _parse(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(address)
    except ValueError:
        return None


def _is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        # Catches what the flags above miss, such as RFC 6598 carrier-grade NAT space.
        or not address.is_global
    )
