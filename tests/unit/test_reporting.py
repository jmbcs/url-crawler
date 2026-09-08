from __future__ import annotations

import io
import json

import pytest

from url_crawler.models import CrawlStats, FetchError, FetchErrorKind, PageResult
from url_crawler.reporting import JsonlReporter, TextReporter


class FlushCountingStream(io.StringIO):
    flushes = 0

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def render_text(result: PageResult) -> str:
    stream = io.StringIO()
    TextReporter(stream).page(result)
    return stream.getvalue()


def render_jsonl(result: PageResult) -> dict[str, object]:
    stream = io.StringIO()
    JsonlReporter(stream).page(result)
    record: dict[str, object] = json.loads(stream.getvalue())
    return record


def test_text_reporter_normal_page() -> None:
    result = PageResult(
        url="https://example.com/",
        status=200,
        links=("https://example.com/about", "https://other.example.org/"),
    )

    assert render_text(result) == (
        "https://example.com/\n  https://example.com/about\n  https://other.example.org/\n\n"
    )


def test_text_reporter_redirect_page() -> None:
    result = PageResult(
        url="https://example.com/old",
        status=301,
        links=("https://example.com/new",),
    )

    assert (
        render_text(result)
        == "https://example.com/old  [redirect 301]\n  https://example.com/new\n\n"
    )


def test_text_reporter_error_page_with_status() -> None:
    result = PageResult(
        url="https://example.com/missing",
        status=404,
        links=(),
        error=FetchError(
            url="https://example.com/missing",
            kind=FetchErrorKind.HTTP_STATUS,
            status=404,
            message="HTTP 404",
            attempts=1,
        ),
    )

    assert render_text(result) == "https://example.com/missing  [error: http_status 404]\n\n"


def test_text_reporter_error_page_without_status() -> None:
    result = PageResult(
        url="https://example.com/slow",
        status=None,
        links=(),
        error=FetchError(
            url="https://example.com/slow",
            kind=FetchErrorKind.TIMEOUT,
            status=None,
            message="timed out",
            attempts=3,
        ),
    )

    assert render_text(result) == "https://example.com/slow  [error: timeout]\n\n"


def test_text_reporter_page_with_no_links() -> None:
    result = PageResult(url="https://example.com/leaf", status=200, links=())

    assert render_text(result) == "https://example.com/leaf\n\n"


def test_text_reporter_finish_writes_nothing() -> None:
    stream = io.StringIO()
    reporter = TextReporter(stream)

    reporter.finish(CrawlStats(), 1.5)

    assert stream.getvalue() == ""


def test_jsonl_reporter_page() -> None:
    result = PageResult(
        url="https://example.com/",
        status=200,
        links=("https://example.com/about",),
    )

    assert render_jsonl(result) == {
        "url": "https://example.com/",
        "status": 200,
        "links": ["https://example.com/about"],
        "error": None,
    }


def test_jsonl_reporter_page_with_error() -> None:
    result = PageResult(
        url="https://example.com/missing",
        status=404,
        links=(),
        error=FetchError(
            url="https://example.com/missing",
            kind=FetchErrorKind.HTTP_STATUS,
            status=404,
            message="HTTP 404",
            attempts=1,
        ),
    )

    assert render_jsonl(result) == {
        "url": "https://example.com/missing",
        "status": 404,
        "links": [],
        "error": {"kind": "http_status", "status": 404, "message": "HTTP 404"},
    }


def test_jsonl_reporter_keeps_non_ascii() -> None:
    result = PageResult(url="https://example.com/café", status=200, links=())

    stream = io.StringIO()
    JsonlReporter(stream).page(result)

    assert "café" in stream.getvalue()


def test_jsonl_reporter_finish() -> None:
    stream = io.StringIO()
    reporter = JsonlReporter(stream)
    stats = CrawlStats(
        pages_ok=3,
        pages_without_links=1,
        redirects=1,
        links_found=10,
        duplicates_dropped=2,
        retries=4,
    )
    stats.pages_failed["timeout"] += 1

    reporter.finish(stats, 2.5)

    assert json.loads(stream.getvalue()) == {
        "summary": {
            "pages_ok": 3,
            "pages_failed": {"timeout": 1},
            "pages_without_links": 1,
            "redirects": 1,
            "links_found": 10,
            "duplicates_dropped": 2,
            "retries": 4,
            "elapsed_seconds": 2.5,
        }
    }


@pytest.mark.parametrize("reporter_cls", [TextReporter, JsonlReporter])
def test_reporter_flushes_after_each_page(
    reporter_cls: type[TextReporter] | type[JsonlReporter],
) -> None:
    stream = FlushCountingStream()
    reporter = reporter_cls(stream)

    reporter.page(PageResult(url="https://example.com/", status=200, links=()))

    assert stream.flushes == 1
