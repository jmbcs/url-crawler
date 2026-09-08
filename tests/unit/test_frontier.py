from __future__ import annotations

import asyncio

from url_crawler.frontier import Frontier


def test_add_then_duplicate_rejected() -> None:
    frontier = Frontier()
    assert frontier.add("https://example.com/a") is True
    assert frontier.add("https://example.com/a") is False


def test_add_rejects_query_order_variant() -> None:
    frontier = Frontier()
    assert frontier.add("https://example.com/a?x=1&y=2") is True
    assert frontier.add("https://example.com/a?y=2&x=1") is False


def test_duplicates_dropped_and_seen_count() -> None:
    frontier = Frontier()
    frontier.add("https://example.com/a")
    frontier.add("https://example.com/a")
    frontier.add("https://example.com/b")
    assert frontier.duplicates_dropped == 1
    assert frontier.seen_count == 2


def test_mark_seen_prevents_later_add() -> None:
    frontier = Frontier()
    assert frontier.mark_seen("https://example.com/a") is True
    assert frontier.add("https://example.com/a") is False
    assert frontier.duplicates_dropped == 1
    assert len(frontier) == 0
    assert frontier.seen_count == 1


def test_mark_seen_returns_false_when_already_seen() -> None:
    frontier = Frontier()
    frontier.add("https://example.com/a")
    assert frontier.mark_seen("https://example.com/a") is False


async def test_len_counts_queued_not_yet_taken() -> None:
    frontier = Frontier()
    frontier.add("https://example.com/a")
    frontier.add("https://example.com/b")
    await frontier.get()
    assert len(frontier) == 1


async def test_add_rejects_url_already_taken_from_queue() -> None:
    frontier = Frontier()
    frontier.add("https://example.com/a")
    url = await frontier.get()
    assert frontier.add(url) is False
    assert frontier.duplicates_dropped == 1
    assert len(frontier) == 0


async def test_get_task_done_join_terminates() -> None:
    frontier = Frontier()
    frontier.add("https://example.com/a")

    async def worker() -> None:
        url = await frontier.get()
        assert url == "https://example.com/a"
        frontier.task_done()

    async with asyncio.TaskGroup() as tg:
        tg.create_task(worker())
        await asyncio.wait_for(frontier.join(), timeout=1.0)


async def test_join_blocks_until_task_done() -> None:
    frontier = Frontier()
    frontier.add("https://example.com/a")
    await frontier.get()

    join_task = asyncio.create_task(frontier.join())
    await asyncio.sleep(0)
    assert not join_task.done()

    frontier.task_done()
    await asyncio.wait_for(join_task, timeout=1.0)
