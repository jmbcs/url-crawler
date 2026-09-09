from __future__ import annotations

import logging
import urllib.robotparser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from url_crawler.fetcher import REDIRECT_STATUSES

log = logging.getLogger(__name__)

MAX_ROBOTS_BYTES = 512_000
MAX_ROBOTS_REDIRECTS = 5


class RobotsPolicy(Protocol):
    @property
    def crawl_delay(self) -> float | None: ...
    def allows(self, url: str) -> bool: ...


class AllowAll:
    @property
    def crawl_delay(self) -> float | None:
        return None

    def allows(self, url: str) -> bool:
        return True


class DenyAll:
    """Applied when robots.txt could not be read: RFC 9309 says an unreachable file blocks."""

    @property
    def crawl_delay(self) -> float | None:
        return None

    def allows(self, url: str) -> bool:
        return False


class RobotsTxt:
    def __init__(self, parser: urllib.robotparser.RobotFileParser, user_agent: str) -> None:
        self._parser = parser
        self._user_agent = user_agent

    @property
    def crawl_delay(self) -> float | None:
        delay = self._parser.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None

    def allows(self, url: str) -> bool:
        return self._parser.can_fetch(self._user_agent, url)


class _UnreachableError(Exception):
    """robots.txt could not be read, so the crawler cannot know what is allowed."""


async def load_robots(client: httpx.AsyncClient, site_url: str, user_agent: str) -> RobotsPolicy:
    split = urlsplit(site_url)
    robots_url = f"{split.scheme}://{split.netloc}/robots.txt"
    try:
        body = await _fetch_robots(client, robots_url)
    except _UnreachableError as exc:
        log.warning(
            "robots.txt at %s is unreachable (%s); blocking the crawl, "
            "pass --ignore-robots to crawl anyway",
            robots_url,
            exc,
        )
        return DenyAll()

    if body is None:
        log.info("robots.txt at %s is unavailable; every path is allowed", robots_url)
        return AllowAll()

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(body.splitlines())
    return RobotsTxt(parser, user_agent)


async def _fetch_robots(client: httpx.AsyncClient, robots_url: str) -> str | None:
    """The robots.txt text, or None when the site says it has none."""
    url = robots_url
    try:
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise _UnreachableError(f"redirect from {url} without a location")
                    url = urljoin(url, location)
                    continue
                if response.status_code >= 500:
                    raise _UnreachableError(f"status {response.status_code}")
                if response.status_code != 200:
                    log.info("robots.txt at %s returned status %s", url, response.status_code)
                    return None
                return _decode(await _read_capped(response), url)
    except httpx.HTTPError as exc:
        raise _UnreachableError(str(exc) or type(exc).__name__) from exc
    raise _UnreachableError(f"more than {MAX_ROBOTS_REDIRECTS} redirects")


async def _read_capped(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) >= MAX_ROBOTS_BYTES:
            break
    return bytes(body[:MAX_ROBOTS_BYTES])


def _decode(body: bytes, url: str) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _UnreachableError(f"{url} is not valid utf-8") from exc
