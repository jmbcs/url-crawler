from __future__ import annotations

import json
from typing import Protocol, TextIO

from url_crawler.models import CrawlStats, PageResult, summary


class Reporter(Protocol):
    def page(self, result: PageResult) -> None: ...
    def finish(self, stats: CrawlStats, elapsed_seconds: float) -> None: ...


class TextReporter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def page(self, result: PageResult) -> None:
        header = result.url
        if result.error is not None:
            status_part = f" {result.error.status}" if result.error.status is not None else ""
            header += f"  [error: {result.error.kind}{status_part}]"
        elif result.status is not None and 300 <= result.status < 400:
            header += f"  [redirect {result.status}]"
        self._stream.write(header + "\n")
        for link in result.links:
            self._stream.write(f"  {link}\n")
        self._stream.write("\n")
        self._stream.flush()

    def finish(self, stats: CrawlStats, elapsed_seconds: float) -> None:
        """Text output has no summary record; the CLI prints the summary to stderr."""


class JsonlReporter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def page(self, result: PageResult) -> None:
        error: dict[str, object] | None = None
        if result.error is not None:
            error = {
                "kind": result.error.kind,
                "status": result.error.status,
                "message": result.error.message,
            }
        record: dict[str, object] = {
            "url": result.url,
            "status": result.status,
            "links": list(result.links),
            "error": error,
        }
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()

    def finish(self, stats: CrawlStats, elapsed_seconds: float) -> None:
        record = {"summary": summary(stats, elapsed_seconds)}
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()
