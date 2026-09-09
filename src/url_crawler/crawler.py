from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from url_crawler.config import CrawlConfig
from url_crawler.fetcher import REDIRECT_STATUSES, Fetcher
from url_crawler.frontier import Frontier
from url_crawler.models import CrawlStats, FetchError, FetchErrorKind, FetchResult, PageResult
from url_crawler.parser import extract_links
from url_crawler.reporting import Reporter
from url_crawler.robots import AllowAll, RobotsPolicy
from url_crawler.urls import HostScope, normalize, resolve_href

log = logging.getLogger(__name__)

FUSE_ERROR_KINDS = frozenset(
    {FetchErrorKind.TIMEOUT, FetchErrorKind.CONNECTION, FetchErrorKind.PROTOCOL}
)
LINKLESS_WARNING_MIN_PAGES = 50
LINKLESS_WARNING_RATIO = 0.9

RobotsLoader = Callable[[str], Awaitable[RobotsPolicy]]
SeedGuard = Callable[[str], Awaitable[str | None]]


class SeedError(Exception):
    """The crawl never started: the seed URL is unusable, unreachable or blocked."""

    def __init__(self, message: str, exit_code: int = 3) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class CrawlOutcome:
    pages: int
    aborted: bool
    reason: str | None


class Crawler:
    """Crawls one host with a pool of workers over a deduplicating frontier."""

    def __init__(
        self,
        fetcher: Fetcher,
        reporter: Reporter,
        config: CrawlConfig,
        stats: CrawlStats,
        *,
        robots_loader: RobotsLoader,
        seed_guard: SeedGuard | None = None,
        extract: Callable[[bytes, str], list[str]] = extract_links,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._fetcher = fetcher
        self._reporter = reporter
        self._config = config
        self._stats = stats
        self._robots_loader = robots_loader
        self._seed_guard = seed_guard
        self._extract = extract
        self._sleep = sleep
        self._frontier = Frontier()
        self._delay_lock = asyncio.Lock()
        self._scope: HostScope | None = None
        self._robots: RobotsPolicy = AllowAll()
        self._claimed = 0
        self._consecutive_failures = 0
        self._stopping = False
        self._aborted = False
        self._reason: str | None = None
        self._delay_logged = False

    @property
    def pending(self) -> int:
        return len(self._frontier)

    async def run(self, seed: str) -> CrawlOutcome:
        start = normalize(seed)
        if start is None:
            raise SeedError(f"Invalid seed URL: {seed}", 2)

        url, result = await self._follow_seed_redirects(start)
        if isinstance(result, FetchError):
            raise SeedError(f"Could not fetch seed {url}: {result.kind} {result.message}")

        self._scope = HostScope.from_url(url)
        if self._scope.host != HostScope.from_url(start).host:
            log.info("scope re-anchored to %s after redirect", self._scope.host)

        self._robots = await self._robots_loader(url) if self._config.respect_robots else AllowAll()
        if not self._robots.allows(url):
            raise SeedError("robots.txt disallows the seed URL; use --ignore-robots to override")

        self._handle(url, result)
        self._claimed = self._stats.pages_total
        await self._run_workers()

        self._warn_if_javascript_rendered()
        return CrawlOutcome(self._stats.pages_total, self._aborted, self._reason)

    async def _follow_seed_redirects(self, seed: str) -> tuple[str, FetchResult | FetchError]:
        """Walk the seed's redirect chain, reporting each hop, and return where it landed."""
        url = seed
        self._frontier.mark_seen(url)
        result = await self._fetch_seed(url)
        for _ in range(self._config.max_seed_redirects):
            if isinstance(result, FetchError):
                return url, result
            target = self._redirect_target(url, result)
            if target is None:
                return url, result
            self._stats.retries += result.attempts - 1
            self._stats.pages_ok += 1
            self._stats.redirects += 1
            self._reporter.page(PageResult(url, result.status, (target,)))
            if not self._frontier.mark_seen(target):
                raise SeedError(f"seed redirect cycle at {target}")
            url = target
            result = await self._fetch_seed(url)
        if isinstance(result, FetchResult) and self._redirect_target(url, result) is not None:
            raise SeedError("Seed redirected too many times")
        return url, result

    async def _fetch_seed(self, url: str) -> FetchResult | FetchError:
        """Let the caller veto a seed host, so the service never crawls a private address."""
        if self._seed_guard is not None:
            reason = await self._seed_guard(url)
            if reason is not None:
                raise SeedError(f"seed rejected: {reason}", 3)
        return await self._fetcher.fetch(url)

    async def _run_workers(self) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                workers = [
                    group.create_task(self._worker()) for _ in range(self._config.concurrency)
                ]
                await self._frontier.join()
                for worker in workers:
                    worker.cancel()
        # A closed stdout hits every worker at once; the CLI wants one error, not a group of ten.
        except* BrokenPipeError as broken:
            raise BrokenPipeError(str(broken.exceptions[0])) from None

    async def _worker(self) -> None:
        while True:
            url = await self._frontier.get()
            try:
                await self._process(url)
            except BrokenPipeError:
                raise
            except Exception as exc:
                log.exception("unexpected error while crawling %s", url)
                self._stats.pages_failed[FetchErrorKind.INTERNAL.value] += 1
                error = FetchError(url, FetchErrorKind.INTERNAL, None, str(exc), 0)
                self._reporter.page(PageResult(url, None, (), error))
            finally:
                self._frontier.task_done()

    async def _process(self, url: str) -> None:
        if self._stopping:
            return
        max_pages = self._config.max_pages
        if max_pages is not None and self._claimed >= max_pages:
            self._stopping = True
            log.warning(
                "stopped at --max-pages %d; %d URLs left unvisited",
                max_pages,
                len(self._frontier) + 1,
            )
            return
        self._claimed += 1
        crawl_delay = self._robots.crawl_delay
        if crawl_delay:
            self._log_crawl_delay_once(crawl_delay)
            async with self._delay_lock:
                await self._sleep(crawl_delay)
        self._handle(url, await self._fetcher.fetch(url))

    def _log_crawl_delay_once(self, crawl_delay: float) -> None:
        if self._delay_logged:
            return
        self._delay_logged = True
        log.warning(
            "robots.txt sets a %ss crawl delay; that limits this crawl to about %.0f pages/hour",
            crawl_delay,
            3600 / crawl_delay,
        )

    def _handle(self, url: str, result: FetchResult | FetchError) -> None:
        self._stats.retries += result.attempts - 1
        if isinstance(result, FetchError):
            self._stats.pages_failed[result.kind.value] += 1
            self._reporter.page(PageResult(url, result.status, (), result))
            self._check_fuse(result)
            return

        self._consecutive_failures = 0
        links: tuple[str, ...]
        if result.status in REDIRECT_STATUSES:
            target = self._redirect_target(url, result)
            links = (target,) if target is not None else ()
            self._stats.redirects += 1
        else:
            links = tuple(self._extract(result.body, url))
            self._stats.links_found += len(links)
            if not links:
                self._stats.pages_without_links += 1
        # Counted last, so a page whose extraction blows up counts as internal only.
        self._stats.pages_ok += 1
        self._reporter.page(PageResult(url, result.status, links))
        self._enqueue(links)

    @staticmethod
    def _redirect_target(url: str, result: FetchResult) -> str | None:
        if result.status not in REDIRECT_STATUSES or not result.location:
            return None
        return resolve_href(result.location, url)

    def _enqueue(self, links: tuple[str, ...]) -> None:
        scope = self._scope
        assert scope is not None, "run() anchors the scope before any page is handled"
        for link in links:
            if scope.allows(link) and self._robots.allows(link):
                self._frontier.add(link)
            else:
                log.debug("printed but not followed: %s", link)
        # Kept live rather than set at the end so an interrupted crawl still reports it.
        self._stats.duplicates_dropped = self._frontier.duplicates_dropped

    def _check_fuse(self, error: FetchError) -> None:
        if self._aborted or not _counts_toward_fuse(error):
            return
        self._consecutive_failures += 1
        if self._consecutive_failures < self._config.failure_fuse:
            return
        self._stopping = True
        self._aborted = True
        self._reason = f"{self._consecutive_failures} consecutive failures"
        log.error("aborting the crawl after %s", self._reason)

    def _warn_if_javascript_rendered(self) -> None:
        pages_ok = self._stats.pages_ok
        if pages_ok < LINKLESS_WARNING_MIN_PAGES:
            return
        if self._stats.pages_without_links / pages_ok > LINKLESS_WARNING_RATIO:
            log.warning("most pages had no links; the site may render its content with JavaScript")


def _counts_toward_fuse(error: FetchError) -> bool:
    if error.kind is FetchErrorKind.HTTP_STATUS:
        return error.status is not None and error.status >= 500
    return error.kind in FUSE_ERROR_KINDS
