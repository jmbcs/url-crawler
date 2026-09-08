from __future__ import annotations

import logging
import urllib.robotparser
from collections.abc import Callable

import httpx
import pytest

from url_crawler.config import CrawlConfig
from url_crawler.robots import MAX_ROBOTS_BYTES, AllowAll, RobotsTxt, load_robots

USER_AGENT = CrawlConfig().user_agent

ROBOTS_BODY = b"""\
User-agent: *
Disallow: /private
Crawl-delay: 2
"""


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_allow_all_allows_everything_and_has_no_crawl_delay() -> None:
    policy = AllowAll()

    assert policy.allows("https://example.com/anything") is True
    assert policy.crawl_delay is None


async def test_load_robots_parses_disallow_and_crawl_delay() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/some/page", USER_AGENT)

    assert requested == ["/robots.txt"]
    assert policy.allows("https://example.com/public") is True
    assert policy.allows("https://example.com/private") is False
    assert policy.crawl_delay == 2.0


async def test_load_robots_returns_allow_all_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, AllowAll)
    assert policy.allows("https://example.com/private") is True


async def test_load_robots_returns_allow_all_on_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, AllowAll)


async def test_load_robots_returns_allow_all_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, AllowAll)


async def test_load_robots_returns_allow_all_on_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/robots-redirected.txt"})

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, AllowAll)


async def test_load_robots_requests_robots_txt_on_site_host() -> None:
    requested: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        await load_robots(client, "https://example.com:8443/deep/page?x=1", USER_AGENT)

    assert len(requested) == 1
    assert str(requested[0]) == "https://example.com:8443/robots.txt"


async def test_load_robots_logs_at_info_on_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with client_for(handler) as client:
        with caplog.at_level(logging.INFO, logger="url_crawler.robots"):
            await load_robots(client, "https://example.com/", USER_AGENT)

    assert "robots.txt" in caplog.text


async def test_load_robots_handles_utf8_bom() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xef\xbb\xbf" + ROBOTS_BODY)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert policy.allows("https://example.com/private") is False
    assert policy.crawl_delay == 2.0


async def test_load_robots_truncates_body_past_cap() -> None:
    padding = "# " + "x" * (MAX_ROBOTS_BYTES + 100_000) + "\n"
    body = f"User-agent: *\nDisallow: /private\n{padding}Disallow: /after-cap\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert policy.allows("https://example.com/private") is False
    assert policy.allows("https://example.com/after-cap") is True


def test_robots_txt_crawl_delay_absent_is_none() -> None:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow:"])
    policy = RobotsTxt(parser, USER_AGENT)

    assert policy.crawl_delay is None
    assert policy.allows("https://example.com/x") is True


def test_robots_txt_allows_delegates_to_parser() -> None:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /secret"])
    policy = RobotsTxt(parser, USER_AGENT)

    assert policy.allows("https://example.com/secret") is False
    assert policy.allows("https://example.com/open") is True


def test_robots_txt_applies_user_agent_specific_group() -> None:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(
        [
            "User-agent: url-crawler",
            "Disallow: /",
            "",
            "User-agent: *",
            "Disallow:",
        ]
    )

    assert RobotsTxt(parser, USER_AGENT).allows("https://example.com/") is False
    assert RobotsTxt(parser, "otherbot/1.0").allows("https://example.com/") is True


async def test_load_robots_returns_allow_all_on_undecodable_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00\x01not utf8")

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, AllowAll)
