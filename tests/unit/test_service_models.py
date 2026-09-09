from __future__ import annotations

from datetime import UTC, datetime

from url_crawler.models import FetchError, FetchErrorKind, PageResult
from url_crawler_service.models import page_row

FETCHED_AT = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)


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
