from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from url_crawler.models import CrawlStats, PageResult


@dataclass(frozen=True, slots=True)
class PageRow:
    seq: int
    url: str
    status: int | None
    error_kind: str | None
    error_message: str | None
    links: tuple[str, ...]
    fetched_at: datetime


def stats_snapshot(stats: CrawlStats, elapsed_seconds: float) -> dict[str, object]:
    return {
        "pages_ok": stats.pages_ok,
        "pages_failed": dict(stats.pages_failed),
        "pages_without_links": stats.pages_without_links,
        "redirects": stats.redirects,
        "links_found": stats.links_found,
        "duplicates_dropped": stats.duplicates_dropped,
        "retries": stats.retries,
        "pages_total": stats.pages_total,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def page_row(seq: int, result: PageResult, fetched_at: datetime) -> PageRow:
    error = result.error
    return PageRow(
        seq=seq,
        url=result.url,
        status=result.status,
        error_kind=None if error is None else error.kind.value,
        error_message=None if error is None else error.message,
        links=result.links,
        fetched_at=fetched_at,
    )
