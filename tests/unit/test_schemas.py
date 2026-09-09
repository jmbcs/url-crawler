from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from url_crawler.config import CrawlConfig
from url_crawler_service.orm import Crawl, CrawlState, Page
from url_crawler_service.schemas import CrawlCreate, CrawlOut, PageOut

CREATED_AT = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


def test_defaults_match_the_cli_defaults() -> None:
    defaults = CrawlConfig()

    assert CrawlCreate(seed="https://example.com").to_config() == {
        "concurrency": defaults.concurrency,
        "timeout": defaults.timeout,
        "max_pages": defaults.max_pages,
        "max_bytes": defaults.max_bytes,
        "respect_robots": defaults.respect_robots,
    }


def test_to_config_builds_a_crawl_config() -> None:
    body = CrawlCreate(
        seed="https://example.com",
        concurrency=4,
        timeout=7.5,
        max_pages=50,
        max_bytes=1024,
        respect_robots=False,
    )

    config = CrawlConfig(**body.to_config())

    assert (config.concurrency, config.timeout, config.max_pages) == (4, 7.5, 50)
    assert (config.max_bytes, config.respect_robots) == (1024, False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 0),
        ("concurrency", 51),
        ("timeout", 0.0),
        ("timeout", 121.0),
        ("max_pages", 0),
        ("max_bytes", 0),
    ],
)
def test_out_of_bounds_values_are_rejected(field: str, value: float) -> None:
    out_of_bounds: dict[str, Any] = {field: value}

    with pytest.raises(ValidationError):
        CrawlCreate(seed="https://example.com", **out_of_bounds)


def test_normalized_seed_defaults_the_scheme_to_https() -> None:
    assert CrawlCreate(seed="example.com").normalized_seed() == "https://example.com/"


def test_normalized_seed_canonicalizes_host_and_path() -> None:
    assert CrawlCreate(seed="HTTP://Example.com:80").normalized_seed() == "http://example.com/"


@pytest.mark.parametrize("seed", ["ftp://example.com", "https://", "http:// example.com"])
def test_normalized_seed_rejects_what_cannot_be_crawled(seed: str) -> None:
    with pytest.raises(ValueError, match=seed.split("://")[0]):
        CrawlCreate(seed=seed).normalized_seed()


def test_crawl_out_reads_an_orm_row() -> None:
    crawl = Crawl(
        id=uuid.uuid4(),
        seed="https://example.com/",
        config={"concurrency": 4},
        state=CrawlState.RUNNING.value,
        created_at=CREATED_AT,
        started_at=CREATED_AT,
        finished_at=None,
        heartbeat_at=CREATED_AT,
        worker_id="host:1",
        attempts=1,
        cancel_requested=False,
        stats={"pages_total": 3},
        error=None,
    )

    out = CrawlOut.model_validate(crawl)

    assert out.id == crawl.id
    assert out.state is CrawlState.RUNNING
    assert out.config == {"concurrency": 4}
    assert out.stats == {"pages_total": 3}
    assert "worker_id" not in out.model_dump()


def test_page_out_nests_the_error_fields() -> None:
    page = Page(
        crawl_id=uuid.uuid4(),
        seq=7,
        url="https://example.com/a",
        status=None,
        error_kind="timeout",
        error_message="read timed out",
        links=[],
        fetched_at=CREATED_AT,
    )

    out = PageOut.from_orm_row(page)

    assert out.error is not None
    assert (out.error.kind, out.error.message) == ("timeout", "read timed out")


def test_page_out_has_no_error_for_a_successful_page() -> None:
    page = Page(
        crawl_id=uuid.uuid4(),
        seq=1,
        url="https://example.com/",
        status=200,
        error_kind=None,
        error_message=None,
        links=["https://example.com/a"],
        fetched_at=CREATED_AT,
    )

    out = PageOut.from_orm_row(page)

    assert out.error is None
    assert out.links == ["https://example.com/a"]


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="max_page"):
        CrawlCreate(seed="https://example.com", max_page=5)  # type: ignore[call-arg]


def test_a_long_seed_is_truncated_in_the_error_message() -> None:
    seed = "http:// " + "a" * 3000

    with pytest.raises(ValueError, match="not a crawlable") as raised:
        CrawlCreate(seed=seed).normalized_seed()

    assert len(str(raised.value)) < 300
    assert "http:// aaa" in str(raised.value)
