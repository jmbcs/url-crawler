from __future__ import annotations

import re

import pytest

from url_crawler import __version__
from url_crawler.config import CrawlConfig


def test_defaults() -> None:
    config = CrawlConfig()

    assert config.concurrency == 10
    assert config.timeout == 10.0
    assert config.max_pages is None
    assert config.max_bytes == 5_000_000
    assert config.respect_robots is True
    assert config.max_seed_redirects == 5
    assert config.failure_fuse == 20
    assert config.user_agent == (
        f"url-crawler/{__version__} (+https://github.com/jmbcs/url-crawler)"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"concurrency": 0},
        {"concurrency": -1},
        {"timeout": 0},
        {"timeout": -1.0},
        {"max_pages": 0},
        {"max_pages": -1},
        {"max_bytes": 0},
        {"max_bytes": -1},
        {"max_seed_redirects": -1},
        {"failure_fuse": 0},
        {"failure_fuse": -1},
    ],
)
def test_invalid_values_raise(kwargs: dict[str, object]) -> None:
    field_name = next(iter(kwargs))
    with pytest.raises(ValueError, match=re.escape(field_name)):
        CrawlConfig(**kwargs)  # type: ignore[arg-type]
