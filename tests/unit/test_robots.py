from __future__ import annotations

import logging
import urllib.robotparser
import zlib
from collections.abc import Callable

import httpx
import pytest

from url_crawler.config import CrawlConfig
from url_crawler.robots import (
    MAX_ROBOTS_BYTES,
    MAX_ROBOTS_REDIRECTS,
    AllowAll,
    DenyAll,
    RobotsTxt,
    load_robots,
)

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


def test_deny_all_blocks_everything_and_has_no_crawl_delay() -> None:
    policy = DenyAll()

    assert policy.allows("https://example.com/anything") is False
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


@pytest.mark.parametrize("status", [401, 403, 404, 410])
async def test_load_robots_returns_allow_all_on_client_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, AllowAll)


async def test_load_robots_returns_deny_all_on_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, DenyAll)
    assert policy.allows("https://example.com/") is False


async def test_load_robots_returns_deny_all_on_connect_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, DenyAll)


async def test_load_robots_follows_redirects_to_the_final_document() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if len(requested) <= 2:
            return httpx.Response(301, headers={"location": f"/hop{len(requested)}.txt"})
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert requested == ["/robots.txt", "/hop1.txt", "/hop2.txt"]
    assert policy.allows("https://example.com/private") is False
    assert policy.crawl_delay == 2.0


async def test_load_robots_follows_a_cross_host_redirect() -> None:
    requested: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if len(requested) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.test/robots.txt"})
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert str(requested[1]) == "https://cdn.test/robots.txt"
    assert policy.allows("https://example.com/private") is False


async def test_load_robots_returns_deny_all_past_the_redirect_limit() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(302, headers={"location": f"/hop{len(requested)}.txt"})

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, DenyAll)
    assert len(requested) == MAX_ROBOTS_REDIRECTS + 1


async def test_load_robots_returns_deny_all_on_a_redirect_without_a_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, DenyAll)


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


async def test_load_robots_warns_when_it_blocks_the_crawl(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with client_for(handler) as client:
        with caplog.at_level(logging.WARNING, logger="url_crawler.robots"):
            await load_robots(client, "https://example.com/", USER_AGENT)

    assert "--ignore-robots" in caplog.text
    assert caplog.records[0].levelname == "WARNING"


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


async def test_load_robots_returns_deny_all_on_undecodable_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00\x01not utf8")

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert isinstance(policy, DenyAll)


METADATA_URL = "http://169.254.169.254/latest/meta-data/"


async def deny_metadata(url: str) -> str | None:
    return "169.254.169.254 is a private address" if "169.254.169.254" in url else None


async def allow_everything(url: str) -> str | None:
    return None


async def test_load_robots_denies_when_a_guard_rejects_a_redirect_hop() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": METADATA_URL})

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT, guard=deny_metadata)

    assert isinstance(policy, DenyAll)
    assert requested == ["https://example.com/robots.txt"]


async def test_load_robots_denies_when_a_guard_rejects_the_initial_url() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        policy = await load_robots(client, METADATA_URL, USER_AGENT, guard=deny_metadata)

    assert isinstance(policy, DenyAll)
    assert requested == []


async def test_load_robots_guards_the_initial_url_and_every_hop() -> None:
    guarded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.test/robots.txt"})
        return httpx.Response(200, content=ROBOTS_BODY)

    async def guard(url: str) -> str | None:
        guarded.append(url)
        return None

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT, guard=guard)

    assert guarded == ["https://example.com/robots.txt", "https://cdn.test/robots.txt"]
    assert policy.allows("https://example.com/private") is False


async def test_load_robots_with_a_permissive_guard_keeps_the_default_behaviour() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if len(requested) == 1:
            return httpx.Response(301, headers={"location": "/hop.txt"})
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        policy = await load_robots(
            client, "https://example.com/", USER_AGENT, guard=allow_everything
        )

    assert requested == ["/robots.txt", "/hop.txt"]
    assert policy.allows("https://example.com/private") is False
    assert policy.crawl_delay == 2.0


async def test_load_robots_warns_with_the_guard_reason(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=ROBOTS_BODY)

    async with client_for(handler) as client:
        with caplog.at_level(logging.WARNING, logger="url_crawler.robots"):
            await load_robots(client, METADATA_URL, USER_AGENT, guard=deny_metadata)

    assert "blocked: 169.254.169.254 is a private address" in caplog.text


async def test_load_robots_reads_a_gzipped_body() -> None:
    compressor = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    compressed = compressor.compress(ROBOTS_BODY) + compressor.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=compressed, headers={"content-encoding": "gzip"})

    async with client_for(handler) as client:
        policy = await load_robots(client, "https://example.com/", USER_AGENT)

    assert policy.allows("https://example.com/private") is False
    assert policy.crawl_delay == 2.0


def policy_for(*lines: str) -> RobotsTxt:
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(list(lines))
    return RobotsTxt(parser, USER_AGENT)


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/x.pdf", False),
        ("/deep/dir/x.pdf", False),
        ("/x.pdf.html", True),
        ("/x.pdfx", True),
        ("/private/a", False),
        ("/private/", False),
        ("/privateer", True),
        ("/public", True),
    ],
)
def test_robots_txt_matches_wildcards_and_end_anchors(path: str, allowed: bool) -> None:
    policy = policy_for("User-agent: *", "Disallow: /*.pdf$", "Disallow: /private/*")

    assert policy.allows(f"https://example.com{path}") is allowed


@pytest.mark.parametrize(
    "lines",
    [
        ("Disallow: /docs", "Allow: /docs/public"),
        ("Allow: /docs/public", "Disallow: /docs"),
    ],
)
def test_robots_txt_prefers_the_longest_matching_rule(lines: tuple[str, str]) -> None:
    policy = policy_for("User-agent: *", *lines)

    assert policy.allows("https://example.com/docs/public/x") is True
    assert policy.allows("https://example.com/docs/private") is False


def test_robots_txt_lets_allow_win_a_tie() -> None:
    policy = policy_for("User-agent: *", "Disallow: /docs", "Allow: /docs")

    assert policy.allows("https://example.com/docs/x") is True


def test_robots_txt_treats_an_empty_disallow_as_allow_all() -> None:
    policy = policy_for("User-agent: *", "Disallow:")

    assert policy.allows("https://example.com/anything") is True


def test_robots_txt_anchors_only_at_a_trailing_dollar() -> None:
    policy = policy_for("User-agent: *", "Disallow: /a$b")

    assert policy.allows("https://example.com/a$b/c") is False


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ("/caf%C3%A9", "/café"),
        ("/café", "/caf%C3%A9"),
        ("/café", "/café"),
        ("/caf%C3%A9", "/caf%C3%A9"),
    ],
)
def test_robots_txt_matches_across_percent_encoding(rule: str, path: str) -> None:
    policy = policy_for("User-agent: *", f"Disallow: {rule}")

    assert policy.allows(f"https://example.com{path}") is False


def test_robots_txt_matches_a_wildcard_in_the_query() -> None:
    policy = policy_for("User-agent: *", "Disallow: /*?session=")

    assert policy.allows("https://example.com/page?session=1") is False
    assert policy.allows("https://example.com/page?q=1") is True


def test_robots_txt_allows_a_path_no_rule_matches() -> None:
    policy = policy_for("User-agent: *", "Disallow: /private")

    assert policy.allows("https://example.com/") is True
