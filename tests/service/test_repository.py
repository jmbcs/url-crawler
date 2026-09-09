from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from url_crawler_service.models import PageRow
from url_crawler_service.orm import Crawl, CrawlState
from url_crawler_service.repository import CrawlRepository, LeaseLost

CONFIG: dict[str, Any] = {
    "concurrency": 4,
    "timeout": 7.5,
    "max_pages": 50,
    "max_bytes": 1024,
    "respect_robots": False,
}


def page(seq: int) -> PageRow:
    return PageRow(
        seq=seq,
        url=f"https://example.com/{seq}",
        status=200,
        error_kind=None,
        error_message=None,
        links=(f"https://example.com/{seq + 1}",),
        fetched_at=datetime.now(UTC),
    )


async def age_heartbeat(engine: AsyncEngine, crawl_id: uuid.UUID, seconds: float) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Crawl)
            .where(Crawl.id == crawl_id)
            .values(heartbeat_at=func.now() - timedelta(seconds=seconds))
        )


async def set_attempts(engine: AsyncEngine, crawl_id: uuid.UUID, attempts: int) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            update(Crawl).where(Crawl.id == crawl_id).values(attempts=attempts)
        )


async def state_of(engine: AsyncEngine, crawl_id: uuid.UUID) -> str:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(Crawl.state).where(Crawl.id == crawl_id))
        ).scalar_one()


async def test_create_then_get_round_trips_the_config(repo: CrawlRepository) -> None:
    created = await repo.create("https://example.com/", CONFIG)

    fetched = await repo.get(created.id)

    assert fetched is not None
    assert fetched.seed == "https://example.com/"
    assert fetched.config == CONFIG
    assert fetched.state == CrawlState.QUEUED
    assert fetched.attempts == 0
    assert fetched.cancel_requested is False
    assert fetched.created_at.tzinfo is not None


async def test_get_returns_none_for_an_unknown_id(repo: CrawlRepository) -> None:
    assert await repo.get(uuid.uuid4()) is None


async def test_list_recent_is_newest_first_and_filters_by_state(repo: CrawlRepository) -> None:
    first = await repo.create("https://a.test/", CONFIG)
    second = await repo.create("https://b.test/", CONFIG)
    await repo.claim("worker-1")

    assert [crawl.id for crawl in await repo.list_recent()] == [second.id, first.id]
    running = await repo.list_recent(state=CrawlState.RUNNING)
    assert [crawl.id for crawl in running] == [first.id]
    assert len(await repo.list_recent(limit=1)) == 1


async def test_claim_takes_the_oldest_queued_crawl_and_marks_it_running(
    repo: CrawlRepository,
) -> None:
    oldest = await repo.create("https://a.test/", CONFIG)
    await repo.create("https://b.test/", CONFIG)

    claimed = await repo.claim("worker-1")

    assert claimed is not None
    assert claimed.id == oldest.id
    assert claimed.state == CrawlState.RUNNING
    assert claimed.worker_id == "worker-1"
    assert claimed.attempts == 1
    assert claimed.started_at is not None
    assert claimed.heartbeat_at is not None


async def test_claim_returns_none_when_nothing_is_queued(repo: CrawlRepository) -> None:
    assert await repo.claim("worker-1") is None


async def test_concurrent_claims_take_different_crawls(repo: CrawlRepository) -> None:
    await repo.create("https://a.test/", CONFIG)
    await repo.create("https://b.test/", CONFIG)

    first, second = await asyncio.gather(repo.claim("worker-1"), repo.claim("worker-2"))

    assert first is not None
    assert second is not None
    assert first.id != second.id


async def test_claim_skips_a_crawl_another_transaction_holds(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    oldest = await repo.create("https://a.test/", CONFIG)
    newest = await repo.create("https://b.test/", CONFIG)

    async with engine.begin() as connection:
        await connection.execute(select(Crawl.id).where(Crawl.id == oldest.id).with_for_update())
        claimed = await repo.claim("worker-2")

    assert claimed is not None
    assert claimed.id == newest.id


async def test_claim_deletes_pages_from_a_previous_attempt(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.insert_pages(crawl.id, "worker-1", [page(1), page(2)])
    await repo.release(crawl.id, "worker-1")

    claimed = await repo.claim("worker-2")

    assert claimed is not None
    assert claimed.attempts == 2
    assert await repo.list_pages(crawl.id) == []


async def test_heartbeat_reports_the_cancel_flag_and_rejects_other_workers(
    repo: CrawlRepository,
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")

    assert await repo.heartbeat(crawl.id, "worker-1", {"pages_total": 1}) is False
    assert await repo.heartbeat(crawl.id, "worker-2", {"pages_total": 1}) is None

    await repo.request_cancel(crawl.id)

    assert await repo.heartbeat(crawl.id, "worker-1", {"pages_total": 2}) is True
    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.stats == {"pages_total": 2}
    assert stored.heartbeat_at is not None


async def test_heartbeat_returns_none_once_the_crawl_is_no_longer_running(
    repo: CrawlRepository,
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.finish(crawl.id, "worker-1", CrawlState.FINISHED, {"pages_total": 3})

    assert await repo.heartbeat(crawl.id, "worker-1", {"pages_total": 4}) is None


async def test_finish_only_applies_to_the_owning_worker(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")

    await repo.finish(crawl.id, "worker-2", CrawlState.FAILED, {}, error="wrong worker")
    assert await state_of(engine, crawl.id) == CrawlState.RUNNING

    await repo.finish(crawl.id, "worker-1", CrawlState.FAILED, {"pages_total": 1}, error="boom")
    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.FAILED
    assert stored.error == "boom"
    assert stored.finished_at is not None


async def test_finish_rejects_a_non_terminal_state(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")

    with pytest.raises(ValueError, match="terminal"):
        await repo.finish(crawl.id, "worker-1", CrawlState.RUNNING, {})


async def test_release_requeues_the_crawl_without_touching_attempts(
    repo: CrawlRepository,
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")

    assert await repo.release(crawl.id, "worker-1") is True

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.QUEUED
    assert stored.worker_id is None
    assert stored.heartbeat_at is None
    assert stored.attempts == 1


async def test_release_refuses_a_crawl_a_cancel_request_raced(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.request_cancel(crawl.id)

    assert await repo.release(crawl.id, "worker-1") is False

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.RUNNING
    assert stored.worker_id == "worker-1"


async def test_reap_requeues_a_stale_crawl_below_max_attempts(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await age_heartbeat(engine, crawl.id, 120)

    assert await repo.reap(lease_seconds=30, max_attempts=3) == 1

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.QUEUED
    assert stored.worker_id is None
    assert stored.attempts == 1


async def test_reap_fails_a_stale_crawl_at_max_attempts(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await age_heartbeat(engine, crawl.id, 120)

    assert await repo.reap(lease_seconds=30, max_attempts=1) == 1

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.FAILED
    assert stored.error == "worker lost"
    assert stored.finished_at is not None


async def test_reap_handles_requeue_and_give_up_in_one_pass(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    retried = await repo.create("https://a.test/", CONFIG)
    exhausted = await repo.create("https://b.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.claim("worker-2")
    await set_attempts(engine, exhausted.id, 3)
    await age_heartbeat(engine, retried.id, 120)
    await age_heartbeat(engine, exhausted.id, 120)

    assert await repo.reap(lease_seconds=30, max_attempts=3) == 2

    requeued = await repo.get(retried.id)
    failed = await repo.get(exhausted.id)
    assert requeued is not None
    assert failed is not None
    assert requeued.state == CrawlState.QUEUED
    assert failed.state == CrawlState.FAILED
    assert failed.error == "worker lost"
    assert failed.worker_id is None


async def test_reap_aborts_a_stale_crawl_the_user_cancelled(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.request_cancel(crawl.id)
    await age_heartbeat(engine, crawl.id, 120)

    assert await repo.reap(lease_seconds=30, max_attempts=3) == 1

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.ABORTED
    assert stored.error == "cancelled by request"
    assert stored.finished_at is not None
    assert stored.worker_id is None


async def test_reap_leaves_a_live_lease_alone(repo: CrawlRepository, engine: AsyncEngine) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")

    assert await repo.reap(lease_seconds=30, max_attempts=3) == 0
    assert await state_of(engine, crawl.id) == CrawlState.RUNNING


async def test_request_cancel_aborts_a_queued_crawl(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)

    assert await repo.request_cancel(crawl.id) == CrawlState.ABORTED

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.ABORTED
    assert stored.error == "cancelled before start"
    assert stored.finished_at is not None


async def test_request_cancel_flags_a_running_crawl(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")

    assert await repo.request_cancel(crawl.id) == CrawlState.RUNNING

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.state == CrawlState.RUNNING
    assert stored.cancel_requested is True


async def test_request_cancel_leaves_a_finished_crawl_unchanged(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.finish(crawl.id, "worker-1", CrawlState.FINISHED, {"pages_total": 1})

    assert await repo.request_cancel(crawl.id) == CrawlState.FINISHED

    stored = await repo.get(crawl.id)
    assert stored is not None
    assert stored.cancel_requested is False


async def test_request_cancel_returns_none_for_an_unknown_id(repo: CrawlRepository) -> None:
    assert await repo.request_cancel(uuid.uuid4()) is None


async def test_insert_pages_and_list_pages_paginate_by_seq(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await repo.insert_pages(crawl.id, "worker-1", [page(seq) for seq in range(1, 6)])

    first = await repo.list_pages(crawl.id, limit=2)
    second = await repo.list_pages(crawl.id, after_seq=first[-1].seq, limit=2)
    third = await repo.list_pages(crawl.id, after_seq=second[-1].seq, limit=2)

    assert [row.seq for row in first] == [1, 2]
    assert [row.seq for row in second] == [3, 4]
    assert [row.seq for row in third] == [5]
    assert first[0].url == "https://example.com/1"
    assert first[0].links == ["https://example.com/2"]
    assert first[0].fetched_at.tzinfo is not None


async def test_insert_pages_accepts_an_empty_batch(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)

    await repo.insert_pages(crawl.id, "worker-1", [])

    assert await repo.list_pages(crawl.id) == []


async def test_insert_pages_stores_failures(repo: CrawlRepository) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    failed = PageRow(
        seq=1,
        url="https://a.test/gone",
        status=404,
        error_kind="http_status",
        error_message="404 Not Found",
        links=(),
        fetched_at=datetime.now(UTC),
    )

    await repo.insert_pages(crawl.id, "worker-1", [failed])

    stored = await repo.list_pages(crawl.id)
    assert stored[0].error_kind == "http_status"
    assert stored[0].error_message == "404 Not Found"
    assert stored[0].links == []


async def test_insert_pages_refuses_a_worker_that_lost_the_lease(
    repo: CrawlRepository, engine: AsyncEngine
) -> None:
    crawl = await repo.create("https://a.test/", CONFIG)
    await repo.claim("worker-1")
    await age_heartbeat(engine, crawl.id, 120)
    await repo.reap(lease_seconds=30, max_attempts=3)
    assert await repo.claim("worker-2") is not None

    with pytest.raises(LeaseLost):
        await repo.insert_pages(crawl.id, "worker-1", [page(1)])

    assert await repo.list_pages(crawl.id) == []


async def test_ping_succeeds(repo: CrawlRepository) -> None:
    await repo.ping()
