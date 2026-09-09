from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

import httpx
import pytest

from tests.conftest import CollectingReporter
from tests.fakesite.site import EXPECTED_CRAWLED, NEVER_REQUESTED, FakeSite
from url_crawler.config import CrawlConfig
from url_crawler.crawler import Crawler, CrawlOutcome, RobotsLoader, SeedError, SeedGuard
from url_crawler.fetcher import Fetcher
from url_crawler.models import CrawlStats, FetchErrorKind, PageResult
from url_crawler.parser import extract_links
from url_crawler.robots import AllowAll, RobotsPolicy, load_robots

pytestmark = pytest.mark.timeout(10)

USER_AGENT = "url-crawler/test"
SEED = "http://site.test/"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}

Handler = Callable[[httpx.Request], httpx.Response]


async def no_sleep(delay: float) -> None:
    return None


class RecordingSleep:
    """Records what it was asked to wait for and yields to the loop instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.live = 0
        self.peak = 0

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(0)
        self.live -= 1


class SlowTransport(httpx.MockTransport):
    """Yields to the loop mid-request, so every worker is in flight before any answer lands."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return await super().handle_async_request(request)


class GaugeTransport(httpx.MockTransport):
    """Tracks how many requests are in flight at once around a fixed per-request delay."""

    def __init__(self, handler: Handler, delay: float = 0.02) -> None:
        super().__init__(handler)
        self._delay = delay
        self.live = 0
        self.peak = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.live += 1
        self.peak = max(self.peak, self.live)
        await asyncio.sleep(self._delay)
        self.live -= 1
        return await super().handle_async_request(request)


class BrokenPipeReporter(CollectingReporter):
    """Stands in for a stdout closed by `| head` after the first page."""

    def page(self, result: PageResult) -> None:
        super().page(result)
        if len(self.pages) > 1:
            raise BrokenPipeError("stdout closed")


class CrawlDelayRobots:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    @property
    def crawl_delay(self) -> float | None:
        return self._delay

    def allows(self, url: str) -> bool:
        return True


async def allow_all(url: str) -> RobotsPolicy:
    return AllowAll()


@dataclass(frozen=True, slots=True)
class Crawl:
    outcome: CrawlOutcome
    stats: CrawlStats
    pages: dict[str, PageResult]
    sleeps: RecordingSleep


async def run_crawl(
    client: httpx.AsyncClient,
    *,
    seed: str = SEED,
    config: CrawlConfig | None = None,
    robots_loader: RobotsLoader = allow_all,
    seed_guard: SeedGuard | None = None,
    extract: Callable[[bytes, str], list[str]] = extract_links,
    max_attempts: int = 3,
    reporter: CollectingReporter | None = None,
    stats: CrawlStats | None = None,
) -> Crawl:
    stats = stats if stats is not None else CrawlStats()
    reporter = reporter if reporter is not None else CollectingReporter()
    sleeps = RecordingSleep()
    fetcher = Fetcher(
        client,
        max_bytes=1_000_000,
        max_attempts=max_attempts,
        sleep=no_sleep,
        rng=random.Random(0),
    )
    crawler = Crawler(
        fetcher,
        reporter,
        config if config is not None else CrawlConfig(),
        stats,
        robots_loader=robots_loader,
        seed_guard=seed_guard,
        extract=extract,
        sleep=sleeps,
    )
    outcome = await crawler.run(seed)
    return Crawl(outcome, stats, {page.url: page for page in reporter.pages}, sleeps)


@asynccontextmanager
async def mock_client(
    handler: Handler,
    *,
    transport: Callable[[Handler], httpx.MockTransport] = httpx.MockTransport,
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=transport(handler), follow_redirects=False) as client:
        yield client


def recording_handler(handler: Handler) -> tuple[Handler, list[str]]:
    urls: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return handler(request)

    return wrapped, urls


def links_html(*targets: str) -> bytes:
    return "".join(f'<a href="{target}">{target}</a>' for target in targets).encode()


@pytest.fixture
async def hazard(fake_client: httpx.AsyncClient) -> Crawl:
    return await run_crawl(
        fake_client,
        robots_loader=partial(load_robots, fake_client, user_agent=USER_AGENT),
    )


async def test_requests_every_expected_path_exactly_once(
    fake_site: FakeSite, hazard: Crawl
) -> None:
    expected = Counter(dict.fromkeys(EXPECTED_CRAWLED, 1))
    expected["/flaky"] = 2
    expected["/robots.txt"] = 1

    assert Counter(fake_site.requested) == expected


async def test_never_requests_blocked_or_off_host_urls(fake_site: FakeSite, hazard: Crawl) -> None:
    assert not NEVER_REQUESTED & set(fake_site.requested)


async def test_root_lists_links_it_never_follows(hazard: Crawl) -> None:
    links = hazard.pages[SEED].links

    assert "http://external.test/x" in links
    assert "http://sub.site.test/x" in links
    assert "http://site.test/robots-blocked" in links


async def test_redirect_page_reports_its_single_target(hazard: Crawl) -> None:
    page = hazard.pages["http://site.test/redirect"]

    assert page.status == 301
    assert page.links == ("http://site.test/redirected",)
    assert page.error is None


async def test_off_site_redirect_target_is_printed_but_not_requested(
    fake_site: FakeSite, hazard: Crawl
) -> None:
    page = hazard.pages["http://site.test/off-site-redirect"]

    assert page.links == ("http://external.test/landing",)
    assert "http://external.test/landing" not in fake_site.requested


async def test_redirect_cycle_visits_each_hop_once(fake_site: FakeSite, hazard: Crawl) -> None:
    second = hazard.pages["http://site.test/redirect-cycle-2"]

    assert second.links == ("http://site.test/redirect-cycle-1",)
    assert fake_site.requested.count("/redirect-cycle-1") == 1
    assert fake_site.requested.count("/redirect-cycle-2") == 1


async def test_pdf_is_reported_as_unsupported_content(hazard: Crawl) -> None:
    page = hazard.pages["http://site.test/file.pdf"]

    assert page.error is not None
    assert page.error.kind is FetchErrorKind.UNSUPPORTED_CONTENT
    assert page.links == ()


async def test_missing_page_is_reported_as_http_status_404(hazard: Crawl) -> None:
    page = hazard.pages["http://site.test/missing"]

    assert page.status == 404
    assert page.error is not None
    assert page.error.kind is FetchErrorKind.HTTP_STATUS
    assert page.error.status == 404


async def test_leaf_page_is_reported_with_no_links(hazard: Crawl) -> None:
    assert hazard.pages["http://site.test/leaf"].links == ()


async def test_flaky_page_succeeds_after_one_retry(fake_site: FakeSite, hazard: Crawl) -> None:
    page = hazard.pages["http://site.test/flaky"]

    assert page.status == 200
    assert page.links == ("http://site.test/flaky-child",)
    assert fake_site.requested.count("/flaky") == 2
    assert hazard.stats.retries == 1


async def test_summary_counts_the_whole_hazard_site(hazard: Crawl) -> None:
    stats = hazard.stats

    assert stats.pages_total == len(EXPECTED_CRAWLED)
    assert stats.pages_ok == 17
    assert dict(stats.pages_failed) == {"http_status": 2, "unsupported_content": 1}
    assert stats.redirects == 4
    # 17 of the links come from /, whose 19 anchors lose mailto: and javascript: and fold #top onto
    # /; the rest are 2+1+1+1+2+1+1 from /a /b /loop /flaky /malformed /base /nofollow.
    assert stats.links_found == 26
    # /leaf, /redirected, /flaky-child, /malformed-child and /nofollow-target.
    assert stats.pages_without_links == 5
    # / and /a twice, then /b, /loop, /malformed and /redirect-cycle-2 pointing back at seen pages.
    assert stats.duplicates_dropped == 7
    assert hazard.outcome == CrawlOutcome(pages=stats.pages_total, aborted=False, reason=None)


async def test_worker_exception_is_isolated(
    fake_client: httpx.AsyncClient, fake_site: FakeSite
) -> None:
    def extract(body: bytes, base_url: str) -> list[str]:
        if base_url == "http://site.test/leaf":
            raise RuntimeError("boom")
        return extract_links(body, base_url)

    crawl = await run_crawl(
        fake_client,
        robots_loader=partial(load_robots, fake_client, user_agent=USER_AGENT),
        extract=extract,
    )

    page = crawl.pages["http://site.test/leaf"]
    assert page.error is not None
    assert page.error.kind is FetchErrorKind.INTERNAL
    assert page.error.message == "boom"
    assert crawl.stats.pages_failed["internal"] == 1
    assert crawl.stats.pages_total == len(EXPECTED_CRAWLED)


async def test_closed_stdout_propagates_instead_of_counting_as_internal(
    fake_client: httpx.AsyncClient,
) -> None:
    stats = CrawlStats()

    with pytest.raises(BrokenPipeError):
        await run_crawl(
            fake_client,
            config=CrawlConfig(respect_robots=False),
            reporter=BrokenPipeReporter(),
            stats=stats,
        )

    assert "internal" not in stats.pages_failed


async def delayed_robots(url: str) -> RobotsPolicy:
    return CrawlDelayRobots(0.25)


async def test_crawl_delay_is_applied_once_per_fetched_page(
    fake_client: httpx.AsyncClient,
) -> None:
    crawl = await run_crawl(fake_client, robots_loader=delayed_robots)

    assert crawl.sleeps.delays == [0.25] * (crawl.stats.pages_total - 1)


async def test_crawl_delay_lock_serialises_the_sleep_across_workers(
    fake_client: httpx.AsyncClient,
) -> None:
    """The hazard site has enough pages that concurrency=8 saturates the worker pool."""
    crawl = await run_crawl(
        fake_client,
        config=CrawlConfig(concurrency=8),
        robots_loader=delayed_robots,
    )

    assert crawl.sleeps.peak == 1


async def test_concurrency_caps_pages_in_flight() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            targets = [f"/p{index}" for index in range(30)]
            return httpx.Response(200, content=links_html(*targets), headers=HTML_HEADERS)
        return httpx.Response(200, content=b"<p>leaf</p>", headers=HTML_HEADERS)

    for concurrency in (1, 5, 10):
        gauge = GaugeTransport(handler)
        async with httpx.AsyncClient(transport=gauge, follow_redirects=False) as client:
            await run_crawl(
                client, config=CrawlConfig(concurrency=concurrency, respect_robots=False)
            )
        assert gauge.peak == concurrency


async def test_stops_at_max_pages_even_with_every_worker_in_flight(
    fake_client: httpx.AsyncClient,
    fake_site: FakeSite,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The crawl-delay sleep suspends all ten workers between claiming a page and fetching it."""
    with caplog.at_level(logging.WARNING, logger="url_crawler.crawler"):
        crawl = await run_crawl(
            fake_client,
            config=CrawlConfig(max_pages=3, concurrency=10),
            robots_loader=delayed_robots,
        )

    assert crawl.stats.pages_total == 3
    assert crawl.outcome == CrawlOutcome(pages=3, aborted=False, reason=None)
    assert len(fake_site.requested) == 3
    assert "stopped at --max-pages 3; 12 URLs left unvisited" in caplog.text


async def test_failure_fuse_aborts_the_crawl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200, content=links_html("/1", "/2", "/3", "/4"), headers=HTML_HEADERS
            )
        return httpx.Response(503)

    async with mock_client(handler) as client:
        crawl = await run_crawl(
            client,
            config=CrawlConfig(concurrency=1, failure_fuse=3, respect_robots=False),
            max_attempts=1,
        )

    assert crawl.outcome == CrawlOutcome(pages=4, aborted=True, reason="3 consecutive failures")
    assert crawl.stats.pages_failed["http_status"] == 3


async def test_failure_fuse_aborts_once_when_every_worker_is_in_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            targets = [f"/{index}" for index in range(10)]
            return httpx.Response(200, content=links_html(*targets), headers=HTML_HEADERS)
        return httpx.Response(503)

    with caplog.at_level(logging.ERROR, logger="url_crawler.crawler"):
        async with mock_client(handler, transport=SlowTransport) as client:
            crawl = await run_crawl(
                client,
                config=CrawlConfig(concurrency=10, failure_fuse=3, respect_robots=False),
                max_attempts=1,
            )

    assert crawl.outcome.aborted is True
    assert crawl.outcome.reason == "3 consecutive failures"
    assert len(caplog.records) == 1


async def test_a_success_resets_the_consecutive_failure_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            targets = [f"/{index}" for index in range(6)]
            return httpx.Response(200, content=links_html(*targets), headers=HTML_HEADERS)
        if int(path.removeprefix("/")) % 2 == 0:
            return httpx.Response(503)
        return httpx.Response(200, content=b"<p>fine</p>", headers=HTML_HEADERS)

    async with mock_client(handler) as client:
        crawl = await run_crawl(
            client,
            config=CrawlConfig(concurrency=1, failure_fuse=2, respect_robots=False),
            max_attempts=1,
        )

    assert crawl.outcome.aborted is False
    assert crawl.stats.pages_failed["http_status"] == 3


async def test_client_errors_never_trip_the_fuse() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            targets = [f"/{index}" for index in range(6)]
            return httpx.Response(200, content=links_html(*targets), headers=HTML_HEADERS)
        return httpx.Response(404)

    async with mock_client(handler) as client:
        crawl = await run_crawl(
            client,
            config=CrawlConfig(concurrency=1, failure_fuse=2, respect_robots=False),
            max_attempts=1,
        )

    assert crawl.outcome.aborted is False
    assert crawl.stats.pages_failed["http_status"] == 6


async def test_invalid_seed_raises_usage_error(fake_client: httpx.AsyncClient) -> None:
    with pytest.raises(SeedError) as excinfo:
        await run_crawl(fake_client, seed="mailto:hello@example.com")

    assert excinfo.value.exit_code == 2


async def test_unreachable_seed_raises_seed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with mock_client(handler) as client:
        with pytest.raises(SeedError, match="Could not fetch seed") as excinfo:
            await run_crawl(client, config=CrawlConfig(respect_robots=False), max_attempts=1)

    assert excinfo.value.exit_code == 3


async def test_robots_disallowed_seed_raises_seed_error(fake_client: httpx.AsyncClient) -> None:
    with pytest.raises(SeedError, match="--ignore-robots") as excinfo:
        await run_crawl(
            fake_client,
            seed="http://site.test/robots-blocked",
            robots_loader=partial(load_robots, fake_client, user_agent=USER_AGENT),
        )

    assert excinfo.value.exit_code == 3


async def test_seed_redirect_chain_longer_than_the_cap_raises_seed_error() -> None:
    hops = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hops
        hops += 1
        return httpx.Response(302, headers={"location": f"/hop{hops}"})

    async with mock_client(handler) as client:
        with pytest.raises(SeedError, match="redirected too many times"):
            await run_crawl(client, config=CrawlConfig(max_seed_redirects=2, respect_robots=False))

    assert hops == 3


async def test_seed_redirect_chain_exactly_at_the_cap_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/landing"})
        return httpx.Response(200, content=b"<p>landing</p>", headers=HTML_HEADERS)

    async with mock_client(handler) as client:
        crawl = await run_crawl(
            client, config=CrawlConfig(max_seed_redirects=1, respect_robots=False)
        )

    assert crawl.pages[SEED].links == ("http://site.test/landing",)
    assert crawl.pages["http://site.test/landing"].status == 200
    assert crawl.stats.pages_total == 2


async def test_seed_redirect_re_anchors_scope_to_the_final_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == SEED:
            return httpx.Response(301, headers={"location": "http://www.site.test/"})
        if url == "http://www.site.test/":
            body = links_html("/x", "http://site.test/old")
            return httpx.Response(200, content=body, headers=HTML_HEADERS)
        return httpx.Response(200, content=b"<p>leaf</p>", headers=HTML_HEADERS)

    recorded, requested = recording_handler(handler)
    with caplog.at_level(logging.INFO, logger="url_crawler.crawler"):
        async with mock_client(recorded) as client:
            crawl = await run_crawl(client, config=CrawlConfig(respect_robots=False))

    assert requested == [SEED, "http://www.site.test/", "http://www.site.test/x"]
    assert crawl.pages[SEED].links == ("http://www.site.test/",)
    assert "http://site.test/old" in crawl.pages["http://www.site.test/"].links
    assert "scope re-anchored to www.site.test" in caplog.text


def redirecting_handler(target: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SEED:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=b"<p>landing</p>", headers=HTML_HEADERS)

    return handler


async def test_seed_guard_rejects_the_seed_before_it_is_fetched() -> None:
    async def guard(url: str) -> str | None:
        return "host resolves to a private address (127.0.0.1)"

    recorded, requested = recording_handler(redirecting_handler("http://elsewhere.test/"))
    async with mock_client(recorded) as client:
        with pytest.raises(SeedError, match="seed rejected: host resolves") as excinfo:
            await run_crawl(client, config=CrawlConfig(respect_robots=False), seed_guard=guard)

    assert excinfo.value.exit_code == 3
    assert requested == []


async def test_seed_guard_rejects_a_redirect_target_after_one_fetch() -> None:
    async def guard(url: str) -> str | None:
        return None if url == SEED else "host resolves to a private address (10.0.0.5)"

    recorded, requested = recording_handler(redirecting_handler("http://elsewhere.test/"))
    async with mock_client(recorded) as client:
        with pytest.raises(SeedError, match="seed rejected: host resolves") as excinfo:
            await run_crawl(client, config=CrawlConfig(respect_robots=False), seed_guard=guard)

    assert excinfo.value.exit_code == 3
    assert requested == [SEED]


async def test_without_a_seed_guard_the_redirect_is_followed() -> None:
    recorded, requested = recording_handler(redirecting_handler("http://elsewhere.test/"))
    async with mock_client(recorded) as client:
        crawl = await run_crawl(client, config=CrawlConfig(respect_robots=False))

    assert requested == [SEED, "http://elsewhere.test/"]
    assert crawl.stats.pages_total == 2


async def test_warns_when_almost_every_page_has_no_links(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            body = links_html(*(f"/p/{index}" for index in range(60)))
            return httpx.Response(200, content=body, headers=HTML_HEADERS)
        return httpx.Response(200, content=b"<p>rendered elsewhere</p>", headers=HTML_HEADERS)

    async with mock_client(handler) as client:
        with caplog.at_level(logging.WARNING, logger="url_crawler.crawler"):
            crawl = await run_crawl(client, config=CrawlConfig(respect_robots=False))

    assert crawl.stats.pages_without_links == 60
    assert "JavaScript" in caplog.text
