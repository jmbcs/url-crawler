from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from url_crawler.config import CrawlConfig
from url_crawler.urls import normalize, prepare_seed
from url_crawler_service.orm import CrawlState, Page

DEFAULTS = CrawlConfig()
MAX_CONCURRENCY = 50
MAX_TIMEOUT_SECONDS = 120.0
MAX_SEED_IN_ERROR = 200


class CrawlCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: str
    concurrency: int = Field(default=DEFAULTS.concurrency, ge=1, le=MAX_CONCURRENCY)
    timeout: float = Field(default=DEFAULTS.timeout, gt=0, le=MAX_TIMEOUT_SECONDS)
    max_pages: int | None = Field(default=DEFAULTS.max_pages, ge=1)
    max_bytes: int = Field(default=DEFAULTS.max_bytes, ge=1)
    respect_robots: bool = DEFAULTS.respect_robots

    def normalized_seed(self) -> str:
        """Apply the CLI seed rules; raises ValueError when the seed is not crawlable."""
        normalized = normalize(prepare_seed(self.seed))
        if normalized is None:
            raise ValueError(f"{self.seed[:MAX_SEED_IN_ERROR]!r} is not a crawlable http(s) URL")
        return normalized

    def to_config(self) -> dict[str, Any]:
        return self.model_dump(exclude={"seed"})


class CrawlOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    seed: str
    state: CrawlState
    config: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    attempts: int
    cancel_requested: bool
    stats: dict[str, Any] | None
    error: str | None


class CrawlList(BaseModel):
    items: list[CrawlOut]


class CrawlErrorOut(BaseModel):
    kind: str
    message: str | None


class PageOut(BaseModel):
    seq: int
    url: str
    status: int | None
    error: CrawlErrorOut | None
    links: list[str]
    fetched_at: datetime

    @classmethod
    def from_orm_row(cls, page: Page) -> PageOut:
        return cls(
            seq=page.seq,
            url=page.url,
            status=page.status,
            error=(
                None
                if page.error_kind is None
                else CrawlErrorOut(kind=page.error_kind, message=page.error_message)
            ),
            links=page.links,
            fetched_at=page.fetched_at,
        )


class PageList(BaseModel):
    items: list[PageOut]
    next_after: int | None


class CancelOut(BaseModel):
    id: UUID
    state: CrawlState
