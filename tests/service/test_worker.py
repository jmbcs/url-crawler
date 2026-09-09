from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.fakesite.app import asgi_app
from tests.fakesite.site import EXPECTED_CRAWLED, FakeSite
from tests.service.conftest import client_factory
from url_crawler.config import CrawlConfig
from url_crawler_service.db import create_engine, make_session_factory
from url_crawler_service.models import PageRow
from url_crawler_service.orm import Crawl, CrawlState
from url_crawler_service.repository import CANCELLED_ERROR, CrawlRepository
from url_crawler_service.settings import Settings
from url_crawler_service.worker import ClientFactory, Worker

WORKER_ID = "worker-under-test"
CONFIG: dict[str, Any] = {"concurrency": 5, "timeout": 5.0, "max_bytes": 1_000_000}
SLOW_SITE_PAGES = 300
REQUEST_DELAY_SECONDS = 0.05
STATE_POLL_SECONDS = 0.05
STATE_POLL_ATTEMPTS = 200
NEVER = 30.0


def build_settings(**overrides: Any) -> Settings:
    defaults = Settings(
        database_url="postgresql+asyncpg://unused/unused",
        page_batch_size=10,
        page_flush_seconds=0.05,
    )
    return replace(defaults, **overrides)


class SlowTransport(httpx.AsyncBaseTransport):
    """Delays every response so a crawl outlives the first heartbeat."""

    def __init__(self, app: Callable[..., Awaitable[None]], delay: float) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self._delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self._delay)
        return await self._inner.handle_async_request(request)


class RecordingRepository(CrawlRepository):
    """Remembers the terminal writes the worker makes."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions)
        self.finished: list[CrawlState] = []

    async def finish(
        self,
        crawl_id: uuid.UUID,
        worker_id: str,
        state: CrawlState,
        stats: dict[str, object],
        error: str | None = None,
    ) -> None:
        self.finished.append(state)
        await super().finish(crawl_id, worker_id, state, stats, error)


class UnreapableRepository(CrawlRepository):
    """Fails its first reap the way a database restart would."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions)
        self.failures = 1

    async def reap(self, lease_seconds: float, max_attempts: int) -> int:
        if self.failures:
            self.failures -= 1
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))
        return await super().reap(lease_seconds, max_attempts)


class UnwritableRepository(CrawlRepository):
    """Rejects every page insert, so the crawl cannot record its results."""

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        raise OperationalError("INSERT INTO page", {}, Exception("disk full"))


def slow_client_factory(site: FakeSite) -> ClientFactory:
    def build(config: CrawlConfig) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=SlowTransport(asgi_app(site), REQUEST_DELAY_SECONDS),
            base_url=f"http://{site.host}",
            follow_redirects=False,
        )

    return build


def unreachable_client_factory() -> ClientFactory:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def build(config: CrawlConfig) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    return build


async def claim_one(repo: CrawlRepository, seed: str) -> tuple[uuid.UUID, Any]:
    crawl = await repo.create(seed, CONFIG)
    claimed = await repo.claim(WORKER_ID)
    assert claimed is not None
    return crawl.id, claimed


async def steal_lease(engine: AsyncEngine, crawl_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Crawl).where(Crawl.id == crawl_id).values(worker_id="another-worker")
        )


async def poll_until(
    repo: CrawlRepository,
    crawl_id: uuid.UUID,
    check: Callable[[Crawl], bool],
    description: str,
) -> None:
    for _ in range(STATE_POLL_ATTEMPTS):
        crawl = await repo.get(crawl_id)
        if crawl is not None and check(crawl):
            return
        await asyncio.sleep(STATE_POLL_SECONDS)
    raise AssertionError(f"the crawl never {description}")


async def wait_for_state(repo: CrawlRepository, crawl_id: uuid.UUID, state: CrawlState) -> None:
    await poll_until(repo, crawl_id, lambda crawl: crawl.state == state, f"reached {state}")


async def wait_for_heartbeat(repo: CrawlRepository, crawl_id: uuid.UUID) -> None:
    await poll_until(repo, crawl_id, lambda crawl: crawl.stats is not None, "heartbeat")


async def test_run_job_crawls_the_fake_site_to_finished(
    repo: CrawlRepository, fake_site: FakeSite
) -> None:
    crawl_id, claimed = await claim_one(repo, f"http://{fake_site.host}/")
    worker = Worker(
        repo, build_settings(), worker_id=WORKER_ID, client_factory=client_factory(fake_site)
    )

    state = await worker.run_job(claimed)

    assert state is CrawlState.FINISHED
    pages = await repo.list_pages(crawl_id, limit=500)
    assert {page.url for page in pages} == {
        f"http://{fake_site.host}{path}" for path in EXPECTED_CRAWLED
    }
    assert len(pages) == len(EXPECTED_CRAWLED)
    assert [page.seq for page in pages] == list(range(1, len(pages) + 1))


async def test_run_job_stores_the_final_stats_snapshot(
    repo: CrawlRepository, fake_site: FakeSite
) -> None:
    crawl_id, claimed = await claim_one(repo, f"http://{fake_site.host}/")
    worker = Worker(
        repo, build_settings(), worker_id=WORKER_ID, client_factory=client_factory(fake_site)
    )

    await worker.run_job(claimed)

    stored = await repo.get(crawl_id)
    assert stored is not None
    assert stored.state == CrawlState.FINISHED
    assert stored.finished_at is not None
    assert stored.error is None
    assert stored.stats is not None
    assert stored.stats["pages_total"] == len(EXPECTED_CRAWLED)


async def test_run_job_aborts_on_a_cancel_request_and_keeps_the_pages(
    repo: CrawlRepository,
) -> None:
    site = FakeSite.generated(SLOW_SITE_PAGES)
    crawl_id, claimed = await claim_one(repo, f"http://{site.host}/")
    worker = Worker(
        repo,
        build_settings(heartbeat_seconds=0.1),
        worker_id=WORKER_ID,
        client_factory=slow_client_factory(site),
    )

    job = asyncio.create_task(worker.run_job(claimed))
    await wait_for_heartbeat(repo, crawl_id)
    assert await repo.request_cancel(crawl_id) is CrawlState.RUNNING
    state = await asyncio.wait_for(job, timeout=10.0)

    assert state is CrawlState.ABORTED
    stored = await repo.get(crawl_id)
    assert stored is not None
    assert stored.state == CrawlState.ABORTED
    assert stored.error == CANCELLED_ERROR
    assert await repo.list_pages(crawl_id, limit=500)


async def test_run_job_gives_up_quietly_when_another_worker_takes_the_lease(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    site = FakeSite.generated(SLOW_SITE_PAGES)
    crawl_id, claimed = await claim_one(repo, f"http://{site.host}/")
    worker = Worker(
        repo,
        # Nothing is flushed before the theft, so any page row means the tail was not discarded.
        build_settings(heartbeat_seconds=0.1, page_batch_size=10_000, page_flush_seconds=NEVER),
        worker_id=WORKER_ID,
        client_factory=slow_client_factory(site),
    )

    job = asyncio.create_task(worker.run_job(claimed))
    await wait_for_heartbeat(repo, crawl_id)
    await steal_lease(engine, crawl_id)
    state = await asyncio.wait_for(job, timeout=10.0)

    assert state is CrawlState.ABORTED
    stored = await repo.get(crawl_id)
    assert stored is not None
    assert stored.state == CrawlState.RUNNING
    assert stored.worker_id == "another-worker"
    assert stored.finished_at is None
    assert await repo.list_pages(crawl_id, limit=1) == []


async def test_run_job_fails_when_the_seed_is_unreachable(repo: CrawlRepository) -> None:
    crawl_id, claimed = await claim_one(repo, "http://site.test/")
    worker = Worker(
        repo, build_settings(), worker_id=WORKER_ID, client_factory=unreachable_client_factory()
    )

    state = await worker.run_job(claimed)

    assert state is CrawlState.FAILED
    stored = await repo.get(crawl_id)
    assert stored is not None
    assert stored.error is not None
    assert stored.error.startswith("Could not fetch seed http://site.test/")


async def test_run_job_fails_when_the_pages_cannot_be_written(
    repo: CrawlRepository, engine: AsyncEngine, fake_site: FakeSite
) -> None:
    unwritable = UnwritableRepository(make_session_factory(engine))
    crawl_id, claimed = await claim_one(unwritable, f"http://{fake_site.host}/")
    worker = Worker(
        unwritable,
        build_settings(),
        worker_id=WORKER_ID,
        client_factory=client_factory(fake_site),
    )

    state = await worker.run_job(claimed)

    assert state is CrawlState.FAILED
    stored = await repo.get(crawl_id)
    assert stored is not None
    assert stored.state == CrawlState.FAILED
    assert stored.error is not None
    assert stored.error.startswith("OperationalError")


async def test_run_job_appends_the_flush_error_to_a_cancelled_crawl(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    site = FakeSite.generated(SLOW_SITE_PAGES)
    unwritable = UnwritableRepository(make_session_factory(engine))
    crawl_id, claimed = await claim_one(unwritable, f"http://{site.host}/")
    worker = Worker(
        unwritable,
        build_settings(heartbeat_seconds=0.1),
        worker_id=WORKER_ID,
        client_factory=slow_client_factory(site),
    )

    job = asyncio.create_task(worker.run_job(claimed))
    await wait_for_heartbeat(repo, crawl_id)
    assert await repo.request_cancel(crawl_id) is CrawlState.RUNNING
    state = await asyncio.wait_for(job, timeout=10.0)

    assert state is CrawlState.ABORTED
    stored = await repo.get(crawl_id)
    assert stored is not None
    assert stored.error is not None
    assert stored.error.startswith(f"{CANCELLED_ERROR}; OperationalError")


async def test_run_job_fails_a_crawl_whose_stored_config_is_unusable(
    repo: CrawlRepository,
) -> None:
    crawl = await repo.create("http://site.test/", CONFIG | {"bogus": 1})
    worker = Worker(repo, build_settings(), worker_id=WORKER_ID)

    assert await worker.run_once() is True

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.FAILED
    assert stored.error is not None
    assert "bogus" in stored.error


async def test_run_once_returns_false_when_the_queue_is_empty(repo: CrawlRepository) -> None:
    worker = Worker(repo, build_settings(), worker_id=WORKER_ID)

    assert await worker.run_once() is False


async def test_run_once_claims_and_finishes_a_queued_crawl(
    repo: CrawlRepository, fake_site: FakeSite
) -> None:
    crawl = await repo.create(f"http://{fake_site.host}/", CONFIG)
    worker = Worker(
        repo, build_settings(), worker_id=WORKER_ID, client_factory=client_factory(fake_site)
    )

    assert await worker.run_once() is True

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.FINISHED
    assert stored.attempts == 1


async def test_run_forever_returns_when_the_stop_event_is_set(repo: CrawlRepository) -> None:
    worker = Worker(repo, build_settings(worker_poll_seconds=30.0), worker_id=WORKER_ID)
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(task, timeout=2.0)


async def test_run_forever_keeps_polling_while_the_database_is_unreachable() -> None:
    engine = create_engine("postgresql+asyncpg://crawler:crawler@localhost:1/crawler")
    worker = Worker(
        CrawlRepository(make_session_factory(engine)),
        build_settings(worker_poll_seconds=0.05),
        worker_id=WORKER_ID,
    )
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(0.3)
    assert not task.done()

    stop.set()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        await engine.dispose()


async def test_run_forever_keeps_polling_after_a_database_error(
    repo: CrawlRepository, engine: AsyncEngine, fake_site: FakeSite
) -> None:
    unreapable = UnreapableRepository(make_session_factory(engine))
    crawl = await repo.create(f"http://{fake_site.host}/", CONFIG)
    worker = Worker(
        unreapable,
        build_settings(worker_poll_seconds=0.05),
        worker_id=WORKER_ID,
        client_factory=client_factory(fake_site),
    )
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop))
    await wait_for_state(repo, crawl.id, CrawlState.FINISHED)
    stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    assert unreapable.failures == 0


async def test_run_forever_requeues_the_running_crawl_when_stopped(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    site = FakeSite.generated(SLOW_SITE_PAGES)
    crawl = await repo.create(f"http://{site.host}/", CONFIG)
    recording = RecordingRepository(make_session_factory(engine))
    worker = Worker(
        recording,
        build_settings(heartbeat_seconds=0.1),
        worker_id=WORKER_ID,
        client_factory=slow_client_factory(site),
    )
    stop = asyncio.Event()

    task = asyncio.create_task(worker.run_forever(stop))
    await wait_for_state(repo, crawl.id, CrawlState.RUNNING)
    stop.set()
    await asyncio.wait_for(task, timeout=10.0)

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.QUEUED
    assert stored.worker_id is None
    assert stored.error is None
    assert stored.finished_at is None
    assert recording.finished == []
