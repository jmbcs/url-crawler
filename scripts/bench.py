"""Benchmark url-crawler's concurrency sweep against a generated fake site."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.fakesite.server import base_url, serve  # noqa: E402
from tests.fakesite.site import FakeSite  # noqa: E402

DEFAULT_PAGES = 300
DEFAULT_DELAY_MS = 50
DEFAULT_CONCURRENCIES = (1, 2, 5, 10, 20)
# Wide enough that the frontier never starves concurrency 20 (verified: narrower
# link graphs cap effective parallelism near the link count, flattening the curve early).
LINKS_PER_PAGE = 50


@dataclass(frozen=True, slots=True)
class BenchRow:
    concurrency: int
    wall_seconds: float
    pages_per_second: float


def run_benchmark(
    pages: int = DEFAULT_PAGES,
    delay_ms: int = DEFAULT_DELAY_MS,
    concurrencies: Sequence[int] = DEFAULT_CONCURRENCIES,
) -> list[BenchRow]:
    server = serve(FakeSite.generated(pages, links_per_page=LINKS_PER_PAGE), delay_ms=delay_ms)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = base_url(server)
        return [_crawl_and_time(url, concurrency, pages) for concurrency in concurrencies]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _crawl_and_time(url: str, concurrency: int, pages: int) -> BenchRow:
    started = time.monotonic()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "url_crawler",
            url,
            "--ignore-robots",
            "--concurrency",
            str(concurrency),
        ],
        stdout=subprocess.DEVNULL,
        cwd=REPO_ROOT,
        check=True,
    )
    wall_seconds = time.monotonic() - started
    # The generated site adds one page for "/" beyond /p/0..pages-1.
    pages_per_second = (pages + 1) / wall_seconds
    return BenchRow(concurrency, wall_seconds, pages_per_second)


def format_table(rows: Sequence[BenchRow], pages: int, delay_ms: int, links_per_page: int) -> str:
    header = (
        f"pages={pages} delay_ms={delay_ms} links_per_page={links_per_page} "
        f"python={platform.python_version()} platform={platform.platform()}"
    )
    lines = [header, "", "| concurrency | wall seconds | pages per second |", "| --- | --- | --- |"]
    lines += [
        f"| {row.concurrency} | {row.wall_seconds:.2f} | {row.pages_per_second:.1f} |"
        for row in rows
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument("--delay-ms", type=int, default=DEFAULT_DELAY_MS)
    args = parser.parse_args()
    rows = run_benchmark(args.pages, args.delay_ms)
    print(format_table(rows, args.pages, args.delay_ms, LINKS_PER_PAGE))


if __name__ == "__main__":
    main()
