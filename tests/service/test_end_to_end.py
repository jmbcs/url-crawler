from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.fakesite.site import EXPECTED_CRAWLED, FakeSite
from tests.service.conftest import client_factory
from url_crawler_service.api import create_app
from url_crawler_service.repository import CANCELLED_BEFORE_START_ERROR, CrawlRepository
from url_crawler_service.settings import Settings
from url_crawler_service.worker import Worker

WORKER_ID = "e2e-worker"
UNUSED_DATABASE_URL = "postgresql+asyncpg://unused/unused"


@pytest.fixture
async def api(repo: CrawlRepository) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(repo)), base_url="http://api.test"
    ) as client:
        yield client


def build_worker(repo: CrawlRepository, fake_site: FakeSite) -> Worker:
    return Worker(
        repo,
        Settings(database_url=UNUSED_DATABASE_URL),
        worker_id=WORKER_ID,
        client_factory=client_factory(fake_site),
    )


async def test_a_posted_crawl_runs_on_the_worker_and_reads_back_over_the_api(
    api: httpx.AsyncClient, repo: CrawlRepository, fake_site: FakeSite
) -> None:
    created = await api.post("/crawls", json={"seed": f"http://{fake_site.host}"})
    assert created.status_code == 202
    body = created.json()
    assert body["state"] == "queued"
    crawl_url = created.headers["location"]

    assert await build_worker(repo, fake_site).run_once() is True

    crawl = (await api.get(crawl_url)).json()
    assert crawl["state"] == "finished"
    assert crawl["error"] is None
    assert crawl["attempts"] == 1
    assert crawl["finished_at"] is not None
    assert crawl["stats"]["pages_total"] == len(EXPECTED_CRAWLED)

    pages = (await api.get(f"{crawl_url}/pages")).json()
    assert {item["url"] for item in pages["items"]} == {
        f"http://{fake_site.host}{path}" for path in EXPECTED_CRAWLED
    }
    assert len(pages["items"]) == len(EXPECTED_CRAWLED)
    assert pages["items"][0]["url"] == body["seed"]
    assert pages["next_after"] is None


async def test_a_crawl_cancelled_before_it_starts_is_never_claimed(
    api: httpx.AsyncClient, repo: CrawlRepository, fake_site: FakeSite
) -> None:
    created = await api.post("/crawls", json={"seed": f"http://{fake_site.host}"})
    crawl_url = created.headers["location"]

    cancelled = await api.delete(crawl_url)
    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "aborted"

    assert await build_worker(repo, fake_site).run_once() is False

    crawl = (await api.get(crawl_url)).json()
    assert crawl["state"] == "aborted"
    assert crawl["error"] == CANCELLED_BEFORE_START_ERROR
    assert (await api.get(f"{crawl_url}/pages")).json()["items"] == []
