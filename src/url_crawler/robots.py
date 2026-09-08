from __future__ import annotations

import logging
import urllib.robotparser
from typing import Protocol
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_ROBOTS_BYTES = 512_000


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


async def load_robots(client: httpx.AsyncClient, site_url: str, user_agent: str) -> RobotsPolicy:
    split = urlsplit(site_url)
    robots_url = f"{split.scheme}://{split.netloc}/robots.txt"
    try:
        async with client.stream("GET", robots_url, follow_redirects=False) as response:
            if response.status_code != 200:
                log.info("robots.txt at %s returned status %s", robots_url, response.status_code)
                return AllowAll()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) >= MAX_ROBOTS_BYTES:
                    break
    except httpx.HTTPError as exc:
        log.info("could not fetch %s: %s", robots_url, exc)
        return AllowAll()

    try:
        text = bytes(body[:MAX_ROBOTS_BYTES]).decode("utf-8-sig")
    except UnicodeDecodeError:
        log.info("robots.txt at %s is not valid utf-8", robots_url)
        return AllowAll()

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    return RobotsTxt(parser, user_agent)
