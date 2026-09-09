from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from url_crawler_service.api import create_app, main
from url_crawler_service.db import create_engine, make_session_factory
from url_crawler_service.models import PageRow
from url_crawler_service.orm import CrawlState
from url_crawler_service.repository import CrawlRepository
from url_crawler_service.schemas import CrawlCreate

CONFIG: dict[str, Any] = CrawlCreate(seed="https://example.com").to_config()
WORKER = "worker-1"


@pytest.fixture
async def api(repo: CrawlRepository) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(repo, events_interval_seconds=0.05)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        yield client


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


async def read_sse(client: httpx.AsyncClient, url: str) -> list[tuple[str, str]]:
    """Read a whole event stream. ASGITransport buffers it, so the crawl must end elsewhere."""
    events: list[tuple[str, str]] = []
    name = ""
    async with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, line.removeprefix("data: ")))
    return events


async def test_post_queues_a_crawl_and_points_at_it(api: httpx.AsyncClient) -> None:
    response = await api.post("/crawls", json={"seed": "https://example.com", "concurrency": 4})

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "queued"
    assert body["seed"] == "https://example.com/"
    assert body["config"]["concurrency"] == 4
    assert response.headers["location"] == f"/crawls/{body['id']}"


async def test_post_defaults_the_seed_scheme_to_https(api: httpx.AsyncClient) -> None:
    response = await api.post("/crawls", json={"seed": "example.com"})

    assert response.json()["seed"] == "https://example.com/"


async def test_post_rejects_a_seed_that_cannot_be_crawled(api: httpx.AsyncClient) -> None:
    response = await api.post("/crawls", json={"seed": "ftp://example.com"})

    assert response.status_code == 422
    assert "ftp" in response.json()["detail"]["seed"]


async def test_post_rejects_an_out_of_range_concurrency(api: httpx.AsyncClient) -> None:
    response = await api.post("/crawls", json={"seed": "example.com", "concurrency": 500})

    assert response.status_code == 422


async def test_post_rejects_a_misspelled_field(api: httpx.AsyncClient) -> None:
    response = await api.post("/crawls", json={"seed": "example.com", "max_page": 5})

    assert response.status_code == 422
    assert "max_page" in response.text


async def test_get_unknown_crawl_is_404(api: httpx.AsyncClient) -> None:
    response = await api.get(f"/crawls/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "crawl not found"}


async def test_get_a_malformed_id_is_422(api: httpx.AsyncClient) -> None:
    assert (await api.get("/crawls/not-a-uuid")).status_code == 422


async def test_list_filters_by_state(api: httpx.AsyncClient, repo: CrawlRepository) -> None:
    await repo.create("https://example.com/", CONFIG)
    await repo.create("https://other.test/", CONFIG)
    claimed = await repo.claim(WORKER)
    assert claimed is not None

    queued = (await api.get("/crawls", params={"state": "queued"})).json()["items"]
    running = (await api.get("/crawls", params={"state": "running"})).json()["items"]

    assert [item["seed"] for item in queued] == ["https://other.test/"]
    assert [item["id"] for item in running] == [str(claimed.id)]
    assert len((await api.get("/crawls")).json()["items"]) == 2


async def test_pages_paginate_by_keyset(api: httpx.AsyncClient, repo: CrawlRepository) -> None:
    crawl = await repo.create("https://example.com/", CONFIG)
    await repo.claim(WORKER)
    await repo.insert_pages(crawl.id, WORKER, [page(seq) for seq in range(1, 251)])

    seen: list[int] = []
    after = 0
    for _ in range(3):
        body = (await api.get(f"/crawls/{crawl.id}/pages", params={"after": after})).json()
        seen.extend(item["seq"] for item in body["items"])
        after = body["next_after"] or 0

    assert seen == list(range(1, 251))
    assert body["next_after"] is None
    assert len(body["items"]) == 50


async def test_pages_of_an_unknown_crawl_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.get(f"/crawls/{uuid.uuid4()}/pages")).status_code == 404


async def test_pages_expose_the_error_of_a_failed_page(
    api: httpx.AsyncClient, repo: CrawlRepository
) -> None:
    crawl = await repo.create("https://example.com/", CONFIG)
    await repo.claim(WORKER)
    failed = PageRow(
        seq=1,
        url="https://example.com/slow",
        status=None,
        error_kind="timeout",
        error_message="read timed out",
        links=(),
        fetched_at=datetime.now(UTC),
    )
    await repo.insert_pages(crawl.id, WORKER, [failed])

    item = (await api.get(f"/crawls/{crawl.id}/pages")).json()["items"][0]

    assert item["error"] == {"kind": "timeout", "message": "read timed out"}
    assert item["status"] is None


async def test_delete_aborts_a_queued_crawl(api: httpx.AsyncClient, repo: CrawlRepository) -> None:
    crawl = await repo.create("https://example.com/", CONFIG)

    response = await api.delete(f"/crawls/{crawl.id}")

    assert response.status_code == 202
    assert response.json() == {"id": str(crawl.id), "state": "aborted"}


async def test_delete_flags_a_running_crawl(api: httpx.AsyncClient, repo: CrawlRepository) -> None:
    await repo.create("https://example.com/", CONFIG)
    claimed = await repo.claim(WORKER)
    assert claimed is not None

    response = await api.delete(f"/crawls/{claimed.id}")

    assert response.json()["state"] == "running"
    assert (await api.get(f"/crawls/{claimed.id}")).json()["cancel_requested"] is True


async def test_delete_unknown_crawl_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.delete(f"/crawls/{uuid.uuid4()}")).status_code == 404


async def test_events_stream_stats_until_the_crawl_ends(
    api: httpx.AsyncClient, repo: CrawlRepository
) -> None:
    await repo.create("https://example.com/", CONFIG)
    claimed = await repo.claim(WORKER)
    assert claimed is not None

    async def finish_soon() -> None:
        await asyncio.sleep(0.5)
        await repo.finish(claimed.id, WORKER, CrawlState.FINISHED, {"pages_total": 3})

    finisher = asyncio.create_task(finish_soon())
    events = await read_sse(api, f"/crawls/{claimed.id}/events")
    await finisher

    names = [name for name, _ in events]
    assert names[0] == "stats"
    assert names.count("stats") >= 2
    assert names[-1] == "end"
    last_stats = json.loads(events[-2][1])
    assert last_stats["state"] == "finished"
    assert last_stats["stats"] == {"pages_total": 3}


async def test_events_of_an_unknown_crawl_is_404(api: httpx.AsyncClient) -> None:
    assert (await api.get(f"/crawls/{uuid.uuid4()}/events")).status_code == 404


async def test_healthz_reports_the_database(api: httpx.AsyncClient) -> None:
    response = await api.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_healthz_is_503_when_the_database_is_down() -> None:
    engine = create_engine("postgresql+asyncpg://crawler:crawler@localhost:1/crawler")
    app = create_app(CrawlRepository(make_session_factory(engine)))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            response = await client.get("/healthz")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_main_reports_a_missing_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert main() == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_main_serves_the_app_on_the_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    served: dict[str, Any] = {}

    def record(app: object, **kwargs: Any) -> None:
        served.update(kwargs, app=app)

    monkeypatch.setattr(uvicorn, "run", record)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://crawler:crawler@localhost:55432/none")
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "9000")

    assert main() == 0
    assert (served["host"], served["port"]) == ("127.0.0.1", 9000)
    assert isinstance(served["app"], FastAPI)
