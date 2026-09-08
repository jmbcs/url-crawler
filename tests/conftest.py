from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.fakesite.app import asgi_app
from tests.fakesite.site import FakeSite
from url_crawler.models import CrawlStats, PageResult


@pytest.fixture
def fake_site() -> FakeSite:
    return FakeSite()


@pytest.fixture
async def fake_client(fake_site: FakeSite) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_app(fake_site)),
        base_url=f"http://{fake_site.host}",
        follow_redirects=False,
    ) as client:
        yield client


class CollectingReporter:
    def __init__(self) -> None:
        self.pages: list[PageResult] = []
        self.finished = False
        self.stats: CrawlStats | None = None
        self.elapsed_seconds: float | None = None

    def page(self, result: PageResult) -> None:
        self.pages.append(result)

    def finish(self, stats: CrawlStats, elapsed_seconds: float) -> None:
        self.finished = True
        self.stats = stats
        self.elapsed_seconds = elapsed_seconds


@pytest.fixture
def collecting_reporter() -> CollectingReporter:
    return CollectingReporter()
