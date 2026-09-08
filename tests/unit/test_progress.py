from __future__ import annotations

import asyncio
import contextlib
import io
from collections import Counter

import pytest

from url_crawler.config import CrawlConfig
from url_crawler.models import CrawlStats
from url_crawler.progress import ERASE_LINE, ProgressLine, format_banner, format_progress


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("stats", "pending", "elapsed_seconds", "expected"),
    [
        (CrawlStats(), 0, 0.0, "pages 0 | queued 0 | links 0 | 0.0 pages/s | 0.0s"),
        (
            CrawlStats(pages_ok=141, pages_failed=Counter({"http_status": 2}), links_found=3904),
            512,
            2.97,
            "pages 143 (2 failed) | queued 512 | links 3,904 | 48.1 pages/s | 3.0s",
        ),
        (
            CrawlStats(pages_ok=12_000, links_found=1_234_567),
            9_876,
            60.0,
            "pages 12,000 | queued 9,876 | links 1,234,567 | 200.0 pages/s | 60.0s",
        ),
        (
            CrawlStats(pages_ok=3, links_found=7),
            1,
            2.0,
            "pages 3 | queued 1 | links 7 | 1.5 pages/s | 2.0s",
        ),
    ],
)
def test_format_progress(
    stats: CrawlStats, pending: int, elapsed_seconds: float, expected: str
) -> None:
    assert format_progress(stats, pending, elapsed_seconds) == expected


@pytest.mark.parametrize(
    ("config", "output_format", "expected_first_line"),
    [
        (
            CrawlConfig(),
            "text",
            "url-crawler 9.9.9: crawling https://example.com with 10 workers "
            "(robots.txt on, text output)",
        ),
        (
            CrawlConfig(concurrency=4, respect_robots=False),
            "jsonl",
            "url-crawler 9.9.9: crawling https://example.com with 4 workers "
            "(robots.txt off, jsonl output)",
        ),
    ],
)
def test_format_banner(config: CrawlConfig, output_format: str, expected_first_line: str) -> None:
    banner = format_banner("https://example.com", config, output_format, "9.9.9")
    assert banner.splitlines() == [
        expected_first_line,
        "Results stream to stdout as pages complete. Ctrl-C stops and keeps what was crawled.",
        "Long or unattended crawl? Run it as a job with url-crawler-api and url-crawler-worker; "
        'see README, "Crawl service".',
    ]


async def test_progress_line_renders_with_the_injected_clock() -> None:
    stream = io.StringIO()
    clock = FakeClock()
    stats = CrawlStats(pages_ok=2, links_found=5)
    progress = ProgressLine(stream, stats, lambda: 3, clock=clock, interval=10.0)
    clock.now = 2.0

    task = asyncio.create_task(progress.run())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    rendered = f"\r{ERASE_LINE}pages 2 | queued 3 | links 5 | 1.0 pages/s | 2.0s"
    assert stream.getvalue() == rendered + f"\r{ERASE_LINE}"


async def test_cancelling_run_clears_the_line() -> None:
    stream = io.StringIO()
    progress = ProgressLine(stream, CrawlStats(), lambda: 0, clock=FakeClock(), interval=10.0)

    task = asyncio.create_task(progress.run())
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert stream.getvalue().endswith(f"\r{ERASE_LINE}")


def test_clear_erases_without_a_render() -> None:
    stream = io.StringIO()
    ProgressLine(stream, CrawlStats(), lambda: 0, clock=FakeClock()).clear()
    assert stream.getvalue() == f"\r{ERASE_LINE}"
