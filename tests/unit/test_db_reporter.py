from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import cast

import pytest

from url_crawler.models import CrawlStats, PageResult
from url_crawler_service.models import PageRow
from url_crawler_service.reporter import DbReporter
from url_crawler_service.repository import CrawlRepository, LeaseLost

CRAWL_ID = uuid.uuid4()
WORKER_ID = "worker-under-test"
NEVER = 60.0
FLUSH_TIMEOUT_SECONDS = 2.0


class FakeRepository:
    """Records the batches a reporter writes, without a database."""

    def __init__(self) -> None:
        self.batches: list[list[PageRow]] = []
        self.wrote = asyncio.Event()

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        assert crawl_id == CRAWL_ID
        assert worker_id == WORKER_ID
        self.batches.append(list(rows))
        self.wrote.set()

    @property
    def rows(self) -> list[PageRow]:
        return [row for batch in self.batches for row in batch]


class BlockingRepository(FakeRepository):
    """Holds an insert open so a close can land while a batch is in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        self.started.set()
        await self.release.wait()
        await super().insert_pages(crawl_id, worker_id, rows)


class FailingRepository(FakeRepository):
    """Rejects the first insert, the way a database blip would."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("insert rejected")
        await super().insert_pages(crawl_id, worker_id, rows)


class UnwritableRepository(FakeRepository):
    """Rejects every insert, the way a database that stays down would."""

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        raise RuntimeError("insert rejected")


class StolenRepository(FakeRepository):
    """Refuses the pages of a crawl another worker now owns."""

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        raise LeaseLost("another worker owns this crawl")


def build_reporter(
    repo: FakeRepository, *, batch_size: int = 100, flush_seconds: float = NEVER
) -> DbReporter:
    return DbReporter(
        cast(CrawlRepository, repo),
        CRAWL_ID,
        worker_id=WORKER_ID,
        batch_size=batch_size,
        flush_seconds=flush_seconds,
    )


def page_result(index: int) -> PageResult:
    return PageResult(f"http://site.test/{index}", 200, (f"http://site.test/{index + 1}",))


@asynccontextmanager
async def flushing(reporter: DbReporter) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(reporter.run())
    try:
        yield task
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def wait_for_flush(repo: FakeRepository) -> None:
    await asyncio.wait_for(repo.wrote.wait(), FLUSH_TIMEOUT_SECONDS)


async def test_flushes_as_soon_as_the_batch_is_full() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo, batch_size=3)

    async with flushing(reporter):
        for index in range(3):
            reporter.page(page_result(index))
        await wait_for_flush(repo)

    assert [len(batch) for batch in repo.batches] == [3]
    assert reporter.pages_written == 3


async def test_flushes_a_partial_batch_on_the_timer() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo, flush_seconds=0.01)

    async with flushing(reporter):
        reporter.page(page_result(0))
        reporter.page(page_result(1))
        await wait_for_flush(repo)

    assert [len(batch) for batch in repo.batches] == [2]


async def test_close_writes_the_tail_without_a_flusher() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo)

    reporter.page(page_result(0))
    reporter.finish(CrawlStats(), 1.5)
    await reporter.close()

    assert [row.url for row in repo.rows] == ["http://site.test/0"]
    assert reporter.pages_written == 1


async def test_sequence_numbers_are_one_based_and_gap_free_across_flushes() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo, batch_size=2)

    async with flushing(reporter):
        reporter.page(page_result(0))
        reporter.page(page_result(1))
        await wait_for_flush(repo)
        for index in range(2, 5):
            reporter.page(page_result(index))
        await reporter.close()

    assert [row.seq for row in repo.rows] == [1, 2, 3, 4, 5]
    assert [len(batch) for batch in repo.batches] == [2, 3]


async def test_pages_written_counts_only_flushed_rows() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo)

    reporter.page(page_result(0))
    assert reporter.pages_written == 0

    await reporter.close()
    assert reporter.pages_written == 1


async def test_page_never_blocks_on_a_buffer_larger_than_the_batch() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo, batch_size=2)

    for index in range(50):
        reporter.page(page_result(index))
    await reporter.close()

    assert [len(batch) for batch in repo.batches] == [50]


async def test_a_cancelled_flush_leaves_its_rows_for_close() -> None:
    repo = BlockingRepository()
    reporter = build_reporter(repo, batch_size=2)

    task = asyncio.create_task(reporter.run())
    reporter.page(page_result(0))
    reporter.page(page_result(1))
    await asyncio.wait_for(repo.started.wait(), FLUSH_TIMEOUT_SECONDS)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    repo.release.set()
    await reporter.close()

    assert [row.seq for row in repo.rows] == [1, 2]
    assert reporter.pages_written == 2


async def test_closing_while_an_insert_is_in_flight_writes_every_page_once() -> None:
    repo = BlockingRepository()
    reporter = build_reporter(repo, batch_size=3)

    task = asyncio.create_task(reporter.run())
    for index in range(3):
        reporter.page(page_result(index))
    await asyncio.wait_for(repo.started.wait(), FLUSH_TIMEOUT_SECONDS)
    reporter.page(page_result(3))
    reporter.page(page_result(4))
    repo.release.set()
    await reporter.close()
    await asyncio.wait_for(task, FLUSH_TIMEOUT_SECONDS)

    assert [row.seq for row in repo.rows] == [1, 2, 3, 4, 5]
    assert reporter.pages_written == 5


async def test_a_failed_insert_is_retried_with_its_rows_on_the_next_flush() -> None:
    repo = FailingRepository()
    reporter = build_reporter(repo, batch_size=2, flush_seconds=0.01)

    async with flushing(reporter):
        for index in range(4):
            reporter.page(page_result(index))
        await wait_for_flush(repo)
        await reporter.close()

    assert [row.seq for row in repo.rows] == [1, 2, 3, 4]
    assert reporter.pages_written == 4


async def test_the_flusher_outlives_failing_inserts_and_close_reports_the_last_one() -> None:
    repo = UnwritableRepository()
    reporter = build_reporter(repo, batch_size=1, flush_seconds=0.01)

    async with flushing(reporter) as task:
        reporter.page(page_result(0))
        await asyncio.sleep(0.05)

        assert not task.done()
        with pytest.raises(RuntimeError):
            await reporter.close()

    assert reporter.pages_written == 0


async def test_a_lost_lease_stops_the_flusher_instead_of_retrying() -> None:
    repo = StolenRepository()
    reporter = build_reporter(repo, batch_size=1, flush_seconds=0.01)

    task = asyncio.create_task(reporter.run())
    reporter.page(page_result(0))
    with pytest.raises(LeaseLost):
        await asyncio.wait_for(task, FLUSH_TIMEOUT_SECONDS)
    with pytest.raises(LeaseLost):
        await reporter.close()

    assert reporter.pages_written == 0


async def test_discard_drops_the_pages_of_a_crawl_we_no_longer_own() -> None:
    repo = FakeRepository()
    reporter = build_reporter(repo)

    reporter.page(page_result(0))
    reporter.discard()
    await reporter.close()

    assert repo.batches == []
    assert reporter.pages_written == 0
