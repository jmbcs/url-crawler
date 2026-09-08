from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from uuid import UUID

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from url_crawler import __version__
from url_crawler_service.db import create_engine, make_session_factory
from url_crawler_service.orm import Crawl, CrawlState
from url_crawler_service.repository import TERMINAL_STATES, CrawlRepository
from url_crawler_service.schemas import (
    CancelOut,
    CrawlCreate,
    CrawlList,
    CrawlOut,
    PageList,
    PageOut,
)
from url_crawler_service.settings import Settings, SettingsError

log = logging.getLogger("url_crawler_service.api")

CRAWL_NOT_FOUND = "crawl not found"
DATABASE_UNAVAILABLE = "database unavailable"
EXIT_OK = 0
EXIT_CONFIG = 2


def create_app(repo: CrawlRepository, *, events_interval_seconds: float = 2.0) -> FastAPI:
    """Build the HTTP API over one repository; the API never crawls, it only reads and writes."""
    app = FastAPI(title="url-crawler service", version=__version__)

    async def get_or_404(crawl_id: UUID) -> Crawl:
        crawl = await repo.get(crawl_id)
        if crawl is None:
            raise HTTPException(status_code=404, detail=CRAWL_NOT_FOUND)
        return crawl

    async def events(crawl_id: UUID) -> AsyncIterator[str]:
        while True:
            crawl = await repo.get(crawl_id)
            if crawl is None:
                break
            yield f"event: stats\ndata: {CrawlOut.from_orm_row(crawl).model_dump_json()}\n\n"
            if CrawlState(crawl.state) in TERMINAL_STATES:
                break
            await asyncio.sleep(events_interval_seconds)
        yield "event: end\ndata: {}\n\n"

    @app.post("/crawls", status_code=202)
    async def create_crawl(body: CrawlCreate, response: Response) -> CrawlOut:
        try:
            seed = body.normalized_seed()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"seed": str(exc)}) from exc
        crawl = await repo.create(seed, body.to_config())
        response.headers["Location"] = f"/crawls/{crawl.id}"
        return CrawlOut.from_orm_row(crawl)

    @app.get("/crawls")
    async def list_crawls(
        state: CrawlState | None = None, limit: int = Query(default=50, ge=1, le=200)
    ) -> CrawlList:
        crawls = await repo.list_recent(state=state, limit=limit)
        return CrawlList(items=[CrawlOut.from_orm_row(crawl) for crawl in crawls])

    @app.get("/crawls/{crawl_id}")
    async def get_crawl(crawl_id: UUID) -> CrawlOut:
        return CrawlOut.from_orm_row(await get_or_404(crawl_id))

    @app.get("/crawls/{crawl_id}/pages")
    async def list_crawl_pages(
        crawl_id: UUID,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> PageList:
        await get_or_404(crawl_id)
        pages = await repo.list_pages(crawl_id, after_seq=after, limit=limit)
        items = [PageOut.from_orm_row(page) for page in pages]
        return PageList(items=items, next_after=items[-1].seq if len(items) == limit else None)

    @app.get("/crawls/{crawl_id}/events")
    async def stream_crawl_events(crawl_id: UUID) -> StreamingResponse:
        await get_or_404(crawl_id)
        return StreamingResponse(
            events(crawl_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.delete("/crawls/{crawl_id}", status_code=202)
    async def cancel_crawl(crawl_id: UUID) -> CancelOut:
        state = await repo.request_cancel(crawl_id)
        if state is None:
            raise HTTPException(status_code=404, detail=CRAWL_NOT_FOUND)
        return CancelOut(id=crawl_id, state=state)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        try:
            await repo.ping()
        except Exception as exc:
            log.warning("health check failed: %s", exc)
            raise HTTPException(status_code=503, detail=DATABASE_UNAVAILABLE) from exc
        return {"status": "ok"}

    return app


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    engine = create_engine(settings.database_url)
    app = create_app(CrawlRepository(make_session_factory(engine)))
    try:
        uvicorn.run(app, host=settings.api_host, port=settings.api_port)
    finally:
        asyncio.run(engine.dispose())
    return EXIT_OK
