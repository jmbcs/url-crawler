from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime

from url_crawler.models import CrawlStats, PageResult
from url_crawler_service.models import PageRow, page_row
from url_crawler_service.repository import CrawlRepository


class DbReporter:
    """Buffers crawled pages and writes them in batches, on a size trigger or a timer."""

    def __init__(
        self,
        repo: CrawlRepository,
        crawl_id: uuid.UUID,
        *,
        batch_size: int,
        flush_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = repo
        self._crawl_id = crawl_id
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._clock = clock
        self._buffer: list[PageRow] = []
        self._seq = 0
        self._pages_written = 0
        self._full = asyncio.Event()
        self._writing = asyncio.Lock()
        self._closing = False

    @property
    def pages_written(self) -> int:
        return self._pages_written

    def page(self, result: PageResult) -> None:
        self._seq += 1
        self._buffer.append(page_row(self._seq, result, self._clock()))
        if len(self._buffer) >= self._batch_size:
            self._full.set()

    def finish(self, stats: CrawlStats, elapsed_seconds: float) -> None:
        """Nothing to do: the worker writes the summary with the terminal state."""

    def discard(self) -> None:
        """Drop the unwritten pages of a crawl another worker now owns."""
        self._buffer.clear()

    async def run(self) -> None:
        """Flush whenever the buffer fills or the timer expires, until close() stops it."""
        while not self._closing:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._full.wait(), self._flush_seconds)
            self._full.clear()
            await self._flush()

    async def close(self) -> None:
        """Stop run() and write whatever is still buffered."""
        self._closing = True
        self._full.set()
        await self._flush()

    async def _flush(self) -> None:
        async with self._writing:
            rows = tuple(self._buffer)
            if not rows:
                return
            # The rows stay buffered until the insert commits, so a failure can be retried.
            await self._repo.insert_pages(self._crawl_id, rows)
            del self._buffer[: len(rows)]
            self._pages_written += len(rows)
