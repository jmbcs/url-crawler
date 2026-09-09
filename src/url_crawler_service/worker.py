from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from functools import partial

import httpx

from url_crawler.config import CrawlConfig
from url_crawler.crawler import Crawler, CrawlOutcome, SeedError
from url_crawler.fetcher import Fetcher
from url_crawler.http import build_client
from url_crawler.models import CrawlStats, summary
from url_crawler.robots import load_robots
from url_crawler_service.db import create_engine, make_session_factory
from url_crawler_service.orm import Crawl, CrawlState
from url_crawler_service.reporter import DbReporter
from url_crawler_service.repository import CANCELLED_ERROR, CrawlRepository, LeaseLostError
from url_crawler_service.settings import Settings, SettingsError

log = logging.getLogger(__name__)

ClientFactory = Callable[[CrawlConfig], httpx.AsyncClient]

EXIT_OK = 0
EXIT_CONFIG = 2
FLUSH_TIMEOUT_SECONDS = 15.0


class _Interrupt(StrEnum):
    """Why the crawl task was cancelled, which decides what the worker records."""

    CANCEL_REQUESTED = "cancel requested"
    LEASE_LOST = "lease lost"
    STOPPED = "worker stopping"


class Worker:
    """Claims one queued crawl at a time and runs it while heartbeating its lease."""

    def __init__(
        self,
        repo: CrawlRepository,
        settings: Settings,
        *,
        worker_id: str,
        client_factory: ClientFactory = build_client,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._worker_id = worker_id
        self._client_factory = client_factory
        self._interrupt: _Interrupt | None = None

    async def run_once(self, *, stop: asyncio.Event | None = None) -> bool:
        """Reap expired leases and run one crawl; False when the queue is empty."""
        reaped = await self._repo.reap(self._settings.lease_seconds, self._settings.max_attempts)
        if reaped:
            log.info("reaped %d crawl(s) whose worker went silent", reaped)
        crawl = await self._repo.claim(self._worker_id)
        if crawl is None:
            return False
        log.info("claimed crawl %s for %s", crawl.id, crawl.seed)
        state = await self.run_job(crawl, stop=stop)
        log.info("crawl %s is now %s", crawl.id, state.value)
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                claimed = await self.run_once(stop=stop)
            except Exception:
                log.exception("the claim loop failed, retrying after the poll interval")
                claimed = False
            if not claimed:
                await self._wait_before_polling(stop)

    async def run_job(self, crawl: Crawl, *, stop: asyncio.Event | None = None) -> CrawlState:
        """Run a claimed crawl to a terminal state and record it against the lease."""
        try:
            config = CrawlConfig(**crawl.config)
        except (TypeError, ValueError) as exc:
            return await self._reject(crawl, exc)

        stats = CrawlStats()
        reporter = DbReporter(
            self._repo,
            crawl.id,
            worker_id=self._worker_id,
            batch_size=self._settings.page_batch_size,
            flush_seconds=self._settings.page_flush_seconds,
        )
        client = self._client_factory(config)
        crawler = Crawler(
            Fetcher(client, max_bytes=config.max_bytes),
            reporter,
            config,
            stats,
            robots_loader=partial(load_robots, client, user_agent=config.user_agent),
        )

        self._interrupt = None
        started = time.monotonic()
        crawl_task = asyncio.create_task(crawler.run(crawl.seed))
        flush_task = asyncio.create_task(reporter.run())
        watchers = [asyncio.create_task(self._heartbeat(crawl, stats, started, crawl_task))]
        if stop is not None:
            watchers.append(asyncio.create_task(self._cancel_on_stop(stop, crawl_task)))

        flush_error: str | None = None
        try:
            try:
                state, error = await self._outcome(crawl, crawl_task)
            finally:
                await self._stop_watchers(crawl, watchers)
                flush_error = await self._drain_pages(crawl, reporter, flush_task)
        finally:
            await client.aclose()

        if flush_error is not None:
            if state is CrawlState.FINISHED:
                state, error = CrawlState.FAILED, flush_error
            elif state is CrawlState.ABORTED:
                error = f"{error}; {flush_error}"
        snapshot = summary(stats, time.monotonic() - started)
        return await self._record(crawl, state, error, snapshot)

    async def _reject(self, crawl: Crawl, exc: Exception) -> CrawlState:
        """Fail a crawl whose stored config no longer builds, rather than crash the worker."""
        error = f"{type(exc).__name__}: {exc}"
        log.error("crawl %s has an unusable config: %s", crawl.id, error)
        snapshot = summary(CrawlStats(), 0.0)
        await self._repo.finish(crawl.id, self._worker_id, CrawlState.FAILED, snapshot, error)
        return CrawlState.FAILED

    async def _outcome(
        self, crawl: Crawl, crawl_task: asyncio.Task[CrawlOutcome]
    ) -> tuple[CrawlState, str | None]:
        try:
            outcome = await crawl_task
        except asyncio.CancelledError:
            if self._interrupt is None:
                crawl_task.cancel()
                await asyncio.gather(crawl_task, return_exceptions=True)
                raise
            log.warning("crawl %s interrupted: %s", crawl.id, self._interrupt.value)
            cancelled = self._interrupt is _Interrupt.CANCEL_REQUESTED
            return CrawlState.ABORTED, CANCELLED_ERROR if cancelled else None
        except SeedError as exc:
            log.warning("crawl %s never started: %s", crawl.id, exc)
            return CrawlState.FAILED, str(exc)
        except Exception as exc:
            log.exception("crawl %s stopped with an unexpected error", crawl.id)
            return CrawlState.FAILED, f"{type(exc).__name__}: {exc}"
        if outcome.aborted:
            return CrawlState.ABORTED, outcome.reason
        return CrawlState.FINISHED, None

    async def _stop_watchers(self, crawl: Crawl, watchers: list[asyncio.Task[None]]) -> None:
        for watcher in watchers:
            watcher.cancel()
        for result in await asyncio.gather(*watchers, return_exceptions=True):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                log.error("crawl %s: a watcher stopped early: %r", crawl.id, result)

    async def _drain_pages(
        self, crawl: Crawl, reporter: DbReporter, flush_task: asyncio.Task[None]
    ) -> str | None:
        """Make the closing flush decide: the flusher retried every earlier failure."""
        if self._interrupt is _Interrupt.LEASE_LOST:
            reporter.discard()
        error: str | None = None
        try:
            async with asyncio.timeout(FLUSH_TIMEOUT_SECONDS):
                await reporter.close()
        except LeaseLostError:
            log.warning("crawl %s: another worker owns it, dropping its pages", crawl.id)
            self._interrupt = _Interrupt.LEASE_LOST
        except Exception as exc:
            log.exception("crawl %s could not write all of its pages", crawl.id)
            error = f"{type(exc).__name__}: {exc}"
        flush_task.cancel()
        await asyncio.gather(flush_task, return_exceptions=True)
        return error

    async def _record(
        self, crawl: Crawl, state: CrawlState, error: str | None, stats: dict[str, object]
    ) -> CrawlState:
        if self._interrupt is _Interrupt.LEASE_LOST:
            log.warning("crawl %s: lease lost, leaving the row to its new owner", crawl.id)
            return state
        if self._interrupt is _Interrupt.STOPPED:
            return await self._requeue(crawl, stats)
        await self._repo.finish(crawl.id, self._worker_id, state, stats, error)
        return state

    async def _requeue(self, crawl: Crawl, stats: dict[str, object]) -> CrawlState:
        """Hand the crawl back to the queue, unless a cancel request raced the shutdown."""
        if await self._repo.release(crawl.id, self._worker_id):
            return CrawlState.QUEUED
        await self._repo.finish(
            crawl.id, self._worker_id, CrawlState.ABORTED, stats, CANCELLED_ERROR
        )
        return CrawlState.ABORTED

    async def _heartbeat(
        self,
        crawl: Crawl,
        stats: CrawlStats,
        started: float,
        crawl_task: asyncio.Task[CrawlOutcome],
    ) -> None:
        silent_seconds = 0.0
        while True:
            await asyncio.sleep(self._settings.heartbeat_seconds)
            snapshot = summary(stats, time.monotonic() - started)
            try:
                cancel_requested = await self._repo.heartbeat(crawl.id, self._worker_id, snapshot)
            except Exception:
                silent_seconds += self._settings.heartbeat_seconds
                log.exception("crawl %s: heartbeat failed", crawl.id)
                # Past the lease the reaper hands the crawl to another worker, so let it go.
                if silent_seconds < self._settings.lease_seconds:
                    continue
                self._cancel(crawl_task, _Interrupt.LEASE_LOST)
                return
            silent_seconds = 0.0
            if cancel_requested is None:
                self._cancel(crawl_task, _Interrupt.LEASE_LOST)
                return
            if cancel_requested:
                self._cancel(crawl_task, _Interrupt.CANCEL_REQUESTED)
                return

    async def _cancel_on_stop(
        self, stop: asyncio.Event, crawl_task: asyncio.Task[CrawlOutcome]
    ) -> None:
        await stop.wait()
        self._cancel(crawl_task, _Interrupt.STOPPED)

    def _cancel(self, crawl_task: asyncio.Task[CrawlOutcome], interrupt: _Interrupt) -> None:
        self._interrupt = interrupt
        crawl_task.cancel()

    async def _wait_before_polling(self, stop: asyncio.Event) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), self._settings.worker_poll_seconds)


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    asyncio.run(_serve(settings))
    return EXIT_OK


async def _serve(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    worker = Worker(CrawlRepository(make_session_factory(engine)), settings, worker_id=worker_id)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stop.set)
    log.info("worker %s polling for crawls", worker_id)
    try:
        await worker.run_forever(stop)
    finally:
        await engine.dispose()
