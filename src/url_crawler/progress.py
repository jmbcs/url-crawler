from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TextIO

from url_crawler.config import CrawlConfig
from url_crawler.models import CrawlStats

ERASE_LINE = "\x1b[K"
JOB_API_NOTICE = (
    "Long or unattended crawl? Run it as a job with url-crawler-api and url-crawler-worker; "
    'see README, "Crawl service".'
)


def format_progress(stats: CrawlStats, pending: int, elapsed_seconds: float) -> str:
    failed = sum(stats.pages_failed.values())
    failures = f" ({failed:,} failed)" if failed else ""
    rate = stats.pages_total / elapsed_seconds if elapsed_seconds > 0 else 0.0
    return (
        f"pages {stats.pages_total:,}{failures} "
        f"| queued {pending:,} | links {stats.links_found:,} "
        f"| {rate:.1f} pages/s | {elapsed_seconds:.1f}s"
    )


def format_banner(seed: str, config: CrawlConfig, output_format: str, version: str) -> str:
    robots = "robots.txt on" if config.respect_robots else "robots.txt off"
    return "\n".join(
        (
            f"url-crawler {version}: crawling {seed} with {config.concurrency} workers "
            f"({robots}, {output_format} output)",
            "Results stream to stdout as pages complete. Ctrl-C stops and keeps what was crawled.",
            JOB_API_NOTICE,
        )
    )


class ProgressLine:
    """Redraws one status line on a terminal stream until it is cancelled."""

    def __init__(
        self,
        stream: TextIO,
        stats: CrawlStats,
        pending: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
        interval: float = 0.3,
    ) -> None:
        self._stream = stream
        self._stats = stats
        self._pending = pending
        self._clock = clock
        self._interval = interval
        self._started = clock()

    async def run(self) -> None:
        try:
            while True:
                self._render()
                await asyncio.sleep(self._interval)
        finally:
            self.clear()

    def clear(self) -> None:
        self._write(f"\r{ERASE_LINE}")

    def _render(self) -> None:
        line = format_progress(self._stats, self._pending(), self._clock() - self._started)
        self._write(f"\r{ERASE_LINE}{line}")

    def _write(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()
