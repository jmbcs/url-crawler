from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from url_crawler_service.models import PageRow
from url_crawler_service.orm import Crawl, CrawlState, Page

TERMINAL_STATES = frozenset({CrawlState.FINISHED, CrawlState.FAILED, CrawlState.ABORTED})
LEASE_LOST_ERROR = "worker lost"
CANCELLED_ERROR = "cancelled by request"
CANCELLED_BEFORE_START_ERROR = "cancelled before start"


class LeaseLost(Exception):  # noqa: N818
    """Raised when a write targets a crawl this worker no longer owns."""


class CrawlRepository:
    """Every crawl and page read or write, each in its own short transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, seed: str, config: dict[str, Any]) -> Crawl:
        crawl = Crawl(seed=seed, config=config)
        async with self._sessions() as session, session.begin():
            session.add(crawl)
            await session.flush()
            await session.refresh(crawl)
        return crawl

    async def get(self, crawl_id: uuid.UUID) -> Crawl | None:
        async with self._sessions() as session, session.begin():
            return await session.get(Crawl, crawl_id)

    async def list_recent(self, *, state: CrawlState | None = None, limit: int = 50) -> list[Crawl]:
        statement = select(Crawl).order_by(Crawl.created_at.desc()).limit(limit)
        if state is not None:
            statement = statement.where(Crawl.state == state.value)
        async with self._sessions() as session, session.begin():
            return list((await session.execute(statement)).scalars())

    async def claim(self, worker_id: str) -> Crawl | None:
        """Take the oldest queued crawl, skipping rows another worker is claiming."""
        queued = (
            select(Crawl.id, Crawl.attempts)
            .where(Crawl.state == CrawlState.QUEUED.value)
            .order_by(Crawl.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        async with self._sessions() as session, session.begin():
            row = (await session.execute(queued)).first()
            if row is None:
                return None
            crawl_id, attempts = row.id, row.attempts
            if attempts > 0:
                await session.execute(delete(Page).where(Page.crawl_id == crawl_id))
            claimed = (
                update(Crawl)
                .where(Crawl.id == crawl_id)
                .values(
                    state=CrawlState.RUNNING.value,
                    started_at=func.now(),
                    heartbeat_at=func.now(),
                    worker_id=worker_id,
                    attempts=attempts + 1,
                )
                .returning(Crawl)
            )
            return (await session.execute(claimed)).scalars().one()

    async def heartbeat(
        self, crawl_id: uuid.UUID, worker_id: str, stats: dict[str, object]
    ) -> bool | None:
        """Refresh the lease and report the cancel flag; None means the lease is gone."""
        statement = (
            update(Crawl)
            .where(
                Crawl.id == crawl_id,
                Crawl.worker_id == worker_id,
                Crawl.state == CrawlState.RUNNING.value,
            )
            .values(heartbeat_at=func.now(), stats=stats)
            .returning(Crawl.cancel_requested)
        )
        async with self._sessions() as session, session.begin():
            return (await session.execute(statement)).scalars().one_or_none()

    async def finish(
        self,
        crawl_id: uuid.UUID,
        worker_id: str,
        state: CrawlState,
        stats: dict[str, object],
        error: str | None = None,
    ) -> None:
        if state not in TERMINAL_STATES:
            raise ValueError(f"{state} is not a terminal state")
        statement = (
            update(Crawl)
            .where(Crawl.id == crawl_id, Crawl.worker_id == worker_id)
            .values(state=state.value, finished_at=func.now(), stats=stats, error=error)
        )
        async with self._sessions() as session, session.begin():
            await session.execute(statement)

    async def release(self, crawl_id: uuid.UUID, worker_id: str) -> None:
        """Requeue a crawl on a graceful worker shutdown, undoing the abort the worker recorded."""
        statement = (
            update(Crawl)
            .where(
                Crawl.id == crawl_id,
                Crawl.worker_id == worker_id,
                Crawl.cancel_requested.is_(False),
            )
            .values(
                state=CrawlState.QUEUED.value,
                worker_id=None,
                heartbeat_at=None,
                finished_at=None,
                error=None,
            )
        )
        async with self._sessions() as session, session.begin():
            await session.execute(statement)

    async def reap(self, lease_seconds: float, max_attempts: int) -> int:
        """Settle crawls whose worker stopped heartbeating: abort, requeue or fail them."""
        expired = (
            Crawl.state == CrawlState.RUNNING.value,
            Crawl.heartbeat_at < func.now() - timedelta(seconds=lease_seconds),
        )
        abort_cancelled = (
            update(Crawl)
            .where(*expired, Crawl.cancel_requested.is_(True))
            .values(
                state=CrawlState.ABORTED.value,
                worker_id=None,
                finished_at=func.now(),
                error=CANCELLED_ERROR,
            )
            .returning(Crawl.id)
        )
        requeue = (
            update(Crawl)
            .where(*expired, Crawl.attempts < max_attempts)
            .values(state=CrawlState.QUEUED.value, worker_id=None, heartbeat_at=None)
            .returning(Crawl.id)
        )
        give_up = (
            update(Crawl)
            .where(*expired)
            .values(
                state=CrawlState.FAILED.value,
                worker_id=None,
                finished_at=func.now(),
                error=LEASE_LOST_ERROR,
            )
            .returning(Crawl.id)
        )
        touched = 0
        async with self._sessions() as session, session.begin():
            # Order matters: each statement leaves fewer rows running for the next one to match.
            for statement in (abort_cancelled, requeue, give_up):
                touched += len((await session.execute(statement)).scalars().all())
        return touched

    async def request_cancel(self, crawl_id: uuid.UUID) -> CrawlState | None:
        """Abort a queued crawl outright, flag a running one, leave a finished one alone."""
        current = select(Crawl.state).where(Crawl.id == crawl_id).with_for_update()
        async with self._sessions() as session, session.begin():
            found = (await session.execute(current)).scalars().one_or_none()
            if found is None:
                return None
            state = CrawlState(found)
            if state is CrawlState.QUEUED:
                await session.execute(
                    update(Crawl)
                    .where(Crawl.id == crawl_id)
                    .values(
                        state=CrawlState.ABORTED.value,
                        finished_at=func.now(),
                        error=CANCELLED_BEFORE_START_ERROR,
                    )
                )
                return CrawlState.ABORTED
            if state is CrawlState.RUNNING:
                await session.execute(
                    update(Crawl).where(Crawl.id == crawl_id).values(cancel_requested=True)
                )
            return state

    async def insert_pages(
        self, crawl_id: uuid.UUID, worker_id: str, rows: Sequence[PageRow]
    ) -> None:
        """Write a batch of pages, but only while this worker still owns the running crawl."""
        if not rows:
            return
        owned = (
            select(Crawl.id)
            .where(
                Crawl.id == crawl_id,
                Crawl.worker_id == worker_id,
                Crawl.state == CrawlState.RUNNING.value,
            )
            .with_for_update(read=True)
        )
        values = [
            {
                "crawl_id": crawl_id,
                "seq": row.seq,
                "url": row.url,
                "status": row.status,
                "error_kind": row.error_kind,
                "error_message": row.error_message,
                "links": list(row.links),
                "fetched_at": row.fetched_at,
            }
            for row in rows
        ]
        async with self._sessions() as session, session.begin():
            if (await session.execute(owned)).first() is None:
                raise LeaseLost(f"crawl {crawl_id} is not running under {worker_id}")
            await session.execute(insert(Page), values)

    async def list_pages(
        self, crawl_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
    ) -> list[Page]:
        statement = (
            select(Page)
            .where(Page.crawl_id == crawl_id, Page.seq > after_seq)
            .order_by(Page.seq)
            .limit(limit)
        )
        async with self._sessions() as session, session.begin():
            return list((await session.execute(statement)).scalars())

    async def ping(self) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(select(1))
