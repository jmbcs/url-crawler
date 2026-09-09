from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from url_crawler.fetcher import REDIRECT_STATUSES, read_bounded

log = logging.getLogger(__name__)

MAX_ROBOTS_BYTES = 512_000
MAX_ROBOTS_REDIRECTS = 5

RobotsGuard = Callable[[str], Awaitable[str | None]]


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


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    length: int
    allowance: bool


@dataclass(slots=True)
class _Group:
    """One robots.txt record: the agent tokens that open it and the lines that follow them."""

    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


class RobotsTxt:
    def __init__(self, body: str, user_agent: str) -> None:
        group = _select_group(_parse_groups(body.splitlines()), user_agent)
        self._rules = group.rules if group is not None else []
        self._crawl_delay = group.crawl_delay if group is not None else None

    @property
    def crawl_delay(self) -> float | None:
        return self._crawl_delay

    def allows(self, url: str) -> bool:
        """RFC 9309 section 2.2.2: the longest matching rule decides, Allow breaking ties."""
        target = _target_path(url)
        matched = [rule for rule in self._rules if rule.pattern.match(target)]
        if not matched:
            return True
        longest = max(rule.length for rule in matched)
        return any(rule.allowance for rule in matched if rule.length == longest)


def _parse_groups(lines: Iterable[str]) -> list[_Group]:
    """RFC 9309 section 2.2.1: consecutive user-agent lines open one group, its rules follow."""
    groups: list[_Group] = []
    naming_agents = False
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            continue
        name, value = parsed
        if name == "user-agent":
            if not naming_agents:
                groups.append(_Group())
            groups[-1].agents.append(value.lower())
            naming_agents = True
            continue
        naming_agents = False
        if not groups:
            continue
        if name in ("allow", "disallow"):
            groups[-1].rules.append(_rule(name, value))
        elif name == "crawl-delay":
            delay = _delay(value)
            if delay is not None:
                groups[-1].crawl_delay = delay
    return groups


def _parse_line(line: str) -> tuple[str, str] | None:
    """A line's lowercased field name and value, or None when it carries neither."""
    name, separator, value = line.split("#", 1)[0].partition(":")
    return (name.strip().lower(), value.strip()) if separator else None


def _rule(name: str, path: str) -> _Rule:
    """An empty `Disallow:` allows everything, which is what RFC 9309 says it means."""
    unquoted = unquote(path)
    return _Rule(_rule_pattern(unquoted), len(unquoted), name == "allow" or not unquoted)


def _delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    return delay if delay > 0 else None


def _select_group(groups: list[_Group], user_agent: str) -> _Group | None:
    """RFC 9309 section 2.2.1: the longest agent token matching ours wins, else the `*` group."""
    token = user_agent.split("/")[0].strip().lower()
    best: _Group | None = None
    best_length = 0
    wildcard: _Group | None = None
    for group in groups:
        for agent in group.agents:
            if agent == "*":
                wildcard = group if wildcard is None else wildcard
            elif agent and token.startswith(agent) and len(agent) > best_length:
                best, best_length = group, len(agent)
    return best if best is not None else wildcard


def _rule_pattern(path: str) -> re.Pattern[str]:
    """RFC 9309 section 2.2.3: * matches any run of characters, a trailing $ anchors the end."""
    anchored = path.endswith("$")
    literal = path.removesuffix("$")
    body = ".*".join(re.escape(part) for part in literal.split("*"))
    return re.compile(f"{body}$" if anchored else body)


def _target_path(url: str) -> str:
    """The path and query a rule is matched against, with percent-escapes folded away."""
    parts = urlsplit(url)
    path = unquote(parts.path) or "/"
    return f"{path}?{unquote(parts.query)}" if parts.query else path


class _UnreachableError(Exception):
    """robots.txt could not be read, so the crawler cannot know what is allowed."""


async def load_robots(
    client: httpx.AsyncClient,
    site_url: str,
    user_agent: str,
    *,
    guard: RobotsGuard | None = None,
) -> RobotsPolicy:
    """The site's robots policy; guard vets the robots URL and every hop before it is fetched."""
    split = urlsplit(site_url)
    robots_url = f"{split.scheme}://{split.netloc}/robots.txt"
    try:
        body = await _fetch_robots(client, robots_url, guard)
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

    return RobotsTxt(body, user_agent)


async def _fetch_robots(
    client: httpx.AsyncClient, robots_url: str, guard: RobotsGuard | None
) -> str | None:
    """The robots.txt text, or None when the site says it has none."""
    url = robots_url
    try:
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            if guard is not None:
                reason = await guard(url)
                if reason is not None:
                    raise _UnreachableError(f"blocked: {reason}")
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
                body, _ = await read_bounded(response, MAX_ROBOTS_BYTES)
                return _decode(body, url)
    except httpx.HTTPError as exc:
        raise _UnreachableError(str(exc) or type(exc).__name__) from exc
    raise _UnreachableError(f"more than {MAX_ROBOTS_REDIRECTS} redirects")


def _decode(body: bytes, url: str) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _UnreachableError(f"{url} is not valid utf-8") from exc
