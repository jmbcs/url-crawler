from __future__ import annotations

import httpx

from url_crawler.config import CrawlConfig

CONNECT_TIMEOUT_SECONDS = 5.0
POOL_TIMEOUT_SECONDS = 5.0


def build_client(config: CrawlConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=config.timeout,
            write=config.timeout,
            pool=POOL_TIMEOUT_SECONDS,
        ),
        limits=httpx.Limits(
            max_connections=config.concurrency,
            max_keepalive_connections=config.concurrency,
        ),
        follow_redirects=False,
        headers={
            "user-agent": config.user_agent,
            # Pinned: httpx offers br and zstd when those libraries are importable, and
            # read_bounded only inflates zlib framings.
            "accept-encoding": "gzip, deflate",
        },
    )
