from __future__ import annotations

from datetime import UTC, datetime

from url_crawler.models import CrawlStats, FetchError, FetchErrorKind, PageResult
from url_crawler_service.models import page_row, stats_snapshot

FETCHED_AT = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


def test_stats_snapshot_is_json_ready() -> None:
    stats = CrawlStats(
        pages_ok=4,
        pages_without_links=1,
        redirects=2,
        links_found=17,
        duplicates_dropped=6,
        retries=3,
    )
    stats.pages_failed[FetchErrorKind.TIMEOUT.value] += 2

    snapshot = stats_snapshot(stats, 1.23456)

    assert snapshot == {
        "pages_ok": 4,
        "pages_failed": {"timeout": 2},
        "pages_without_links": 1,
        "redirects": 2,
        "links_found": 17,
        "duplicates_dropped": 6,
        "retries": 3,
        "pages_total": 6,
        "elapsed_seconds": 1.235,
    }
    failed = snapshot["pages_failed"]
    assert isinstance(failed, dict)
    assert all(type(kind) is str for kind in failed)


def test_stats_snapshot_of_an_empty_crawl() -> None:
    snapshot = stats_snapshot(CrawlStats(), 0.0)

    assert snapshot["pages_failed"] == {}
    assert snapshot["pages_total"] == 0
    assert snapshot["elapsed_seconds"] == 0.0


def test_page_row_from_a_successful_page() -> None:
    result = PageResult(url="https://a.test/", status=200, links=("https://a.test/b",))

    row = page_row(1, result, FETCHED_AT)

    assert row.seq == 1
    assert row.url == "https://a.test/"
    assert row.status == 200
    assert row.error_kind is None
    assert row.error_message is None
    assert row.links == ("https://a.test/b",)
    assert row.fetched_at == FETCHED_AT


def test_page_row_from_a_failed_page_stores_plain_strings() -> None:
    error = FetchError(
        url="https://a.test/x",
        kind=FetchErrorKind.HTTP_STATUS,
        status=503,
        message="server error 503",
        attempts=3,
    )
    result = PageResult(url="https://a.test/x", status=503, links=(), error=error)

    row = page_row(7, result, FETCHED_AT)

    assert row.error_kind == "http_status"
    assert type(row.error_kind) is str
    assert row.error_message == "server error 503"
    assert row.links == ()
