from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from url_crawler.models import PageResult


@dataclass(frozen=True, slots=True)
class PageRow:
    seq: int
    url: str
    status: int | None
    error_kind: str | None
    error_message: str | None
    links: tuple[str, ...]
    fetched_at: datetime


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
