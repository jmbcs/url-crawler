from __future__ import annotations

from dataclasses import dataclass

from url_crawler import __version__


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    concurrency: int = 10
    timeout: float = 10.0
    max_pages: int | None = None
    max_bytes: int = 5_000_000
    respect_robots: bool = True
    max_seed_redirects: int = 5
    failure_fuse: int = 20
    user_agent: str = f"url-crawler/{__version__} (+https://github.com/jmbcs/url-crawler)"

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {self.concurrency}")
        if self.timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {self.timeout}")
        if self.max_pages is not None and self.max_pages < 1:
            raise ValueError(f"max_pages must be >= 1 when set, got {self.max_pages}")
        if self.max_bytes < 1:
            raise ValueError(f"max_bytes must be >= 1, got {self.max_bytes}")
        if self.max_seed_redirects < 0:
            raise ValueError(f"max_seed_redirects must be >= 0, got {self.max_seed_redirects}")
        if self.failure_fuse < 1:
            raise ValueError(f"failure_fuse must be >= 1, got {self.failure_fuse}")
