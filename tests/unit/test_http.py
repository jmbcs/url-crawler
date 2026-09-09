from __future__ import annotations

import httpx

from url_crawler.config import CrawlConfig
from url_crawler.http import CONNECT_TIMEOUT_SECONDS, POOL_TIMEOUT_SECONDS, build_client


async def test_build_client_disables_redirects_and_sets_user_agent() -> None:
    config = CrawlConfig(user_agent="test-agent/1.0")
    async with build_client(config) as client:
        assert client.follow_redirects is False
        assert client.headers["user-agent"] == "test-agent/1.0"


async def test_build_client_uses_config_and_module_timeouts() -> None:
    config = CrawlConfig(timeout=7.5)
    async with build_client(config) as client:
        assert client.timeout == httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=7.5,
            write=7.5,
            pool=POOL_TIMEOUT_SECONDS,
        )


async def test_build_client_pools_connections_to_concurrency() -> None:
    config = CrawlConfig(concurrency=3)
    async with build_client(config) as client:
        # httpx exposes no public accessor for the configured limits.
        pool = client._transport._pool  # type: ignore[attr-defined]
        assert pool._max_connections == 3
        assert pool._max_keepalive_connections == 3


async def test_build_client_advertises_only_the_encodings_it_can_inflate() -> None:
    async with build_client(CrawlConfig()) as client:
        assert client.headers["accept-encoding"] == "gzip, deflate"
