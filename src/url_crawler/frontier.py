from __future__ import annotations

import asyncio

from url_crawler.urls import canonical_key


class Frontier:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._seen: set[str] = set()
        self._duplicates_dropped = 0

    def add(self, url: str) -> bool:
        key = canonical_key(url)
        if key in self._seen:
            self._duplicates_dropped += 1
            return False
        self._seen.add(key)
        self._queue.put_nowait(url)
        return True

    def mark_seen(self, url: str) -> bool:
        key = canonical_key(url)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    async def get(self) -> str:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    @property
    def duplicates_dropped(self) -> int:
        return self._duplicates_dropped

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def __len__(self) -> int:
        return self._queue.qsize()
