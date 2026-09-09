from __future__ import annotations

from url_crawler.models import CrawlStats, FetchErrorKind, summary


def test_summary_is_json_ready() -> None:
    stats = CrawlStats(
        pages_ok=4,
        pages_without_links=1,
        redirects=2,
        links_found=17,
        duplicates_dropped=6,
        retries=3,
    )
    stats.pages_failed[FetchErrorKind.TIMEOUT.value] += 2

    snapshot = summary(stats, 1.23456)

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


def test_summary_of_an_empty_crawl() -> None:
    snapshot = summary(CrawlStats(), 0.0)

    assert snapshot["pages_failed"] == {}
    assert snapshot["pages_total"] == 0
    assert snapshot["elapsed_seconds"] == 0.0
