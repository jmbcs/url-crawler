from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum


class FetchErrorKind(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    HTTP_STATUS = "http_status"
    UNSUPPORTED_CONTENT = "unsupported_content"
    TOO_LARGE = "too_large"
    INVALID_URL = "invalid_url"
    PROTOCOL = "protocol"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    body: bytes
    content_type: str
    location: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class FetchError:
    url: str
    kind: FetchErrorKind
    status: int | None
    message: str
    attempts: int


@dataclass(frozen=True, slots=True)
class PageResult:
    url: str
    status: int | None
    links: tuple[str, ...]
    error: FetchError | None = None


@dataclass(slots=True)
class CrawlStats:
    pages_ok: int = 0
    pages_failed: Counter[str] = field(default_factory=Counter)
    pages_without_links: int = 0
    redirects: int = 0
    links_found: int = 0
    duplicates_dropped: int = 0
    retries: int = 0

    @property
    def pages_total(self) -> int:
        return self.pages_ok + sum(self.pages_failed.values())
