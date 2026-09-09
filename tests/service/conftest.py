from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.fakesite.app import asgi_app
from tests.fakesite.site import FakeSite
from url_crawler.config import CrawlConfig
from url_crawler_service.db import make_session_factory
from url_crawler_service.repository import CrawlRepository

DATABASE_URL_ENV = "URL_CRAWLER_TEST_DATABASE_URL"
REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_TESTS = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if SERVICE_TESTS in item.path.parents:
            item.add_marker(pytest.mark.postgres)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        pytest.skip(f"set {DATABASE_URL_ENV} to run the service tests")
    return url


@contextmanager
def alembic_env(database_url: str) -> Iterator[Config]:
    """Alembic config with DATABASE_URL exported for env.py, restored on exit."""
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", database_url)
    try:
        yield Config(str(REPO_ROOT / "alembic.ini"))
    finally:
        patch.undo()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[AsyncEngine]:
    with alembic_env(database_url) as config:
        command.upgrade(config, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
async def repo(engine: AsyncEngine) -> CrawlRepository:
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE page, crawl"))
    return CrawlRepository(make_session_factory(engine))


async def allow_any_host(url: str) -> str | None:
    """The fake hosts resolve nowhere, so the tests turn the private-host guard off."""
    return None


def client_factory(fake_site: FakeSite) -> Callable[[CrawlConfig], httpx.AsyncClient]:
    """Build the fake-site client the worker uses instead of a real HTTP client."""

    def build(config: CrawlConfig) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app(fake_site)),
            base_url=f"http://{fake_site.host}",
            follow_redirects=False,
        )

    return build
