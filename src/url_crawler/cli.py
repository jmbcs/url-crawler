from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from functools import partial

from url_crawler import __version__
from url_crawler.config import CrawlConfig
from url_crawler.crawler import Crawler, CrawlOutcome, SeedError
from url_crawler.fetcher import Fetcher
from url_crawler.http import build_client
from url_crawler.models import CrawlStats
from url_crawler.progress import ProgressLine, format_banner
from url_crawler.reporting import JsonlReporter, Reporter, TextReporter
from url_crawler.robots import load_robots
from url_crawler.urls import ALLOWED_SCHEMES

log = logging.getLogger("url_crawler.cli")

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_ABORTED = 4
EXIT_INTERRUPTED = 130

LOG_LEVELS = (logging.WARNING, logging.INFO, logging.DEBUG)
CANCEL_SIGNALS = (signal.SIGINT, signal.SIGTERM)
DEFAULTS = CrawlConfig()


def prepare_seed(url: str) -> str:
    """Default a missing scheme to https and reject anything but http(s)."""
    if "://" not in url:
        return f"https://{url}"
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme {scheme!r}: use http:// or https://")
    return url


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=LOG_LEVELS[min(args.verbose, len(LOG_LEVELS) - 1)],
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        seed = prepare_seed(args.url)
        config = CrawlConfig(
            concurrency=args.concurrency,
            timeout=args.timeout,
            max_pages=args.max_pages,
            max_bytes=args.max_bytes,
            respect_robots=not args.ignore_robots,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if seed != args.url:
        log.info("assuming https:// for %s", args.url)
    stderr_isatty = sys.stderr.isatty()
    if _banner_enabled(args, stderr_isatty):
        print(format_banner(seed, config, args.format, __version__), file=sys.stderr)

    stats = CrawlStats()
    reporter: Reporter = (
        JsonlReporter(sys.stdout) if args.format == "jsonl" else TextReporter(sys.stdout)
    )

    started = time.monotonic()
    try:
        exit_code = asyncio.run(
            _crawl(seed, config, stats, reporter, progress=_progress_enabled(args, stderr_isatty))
        )
    except SeedError as exc:
        log.error("%s", exc)
        exit_code = exc.exit_code
    except BrokenPipeError:
        _discard_stdout()
        exit_code = EXIT_OK
    except KeyboardInterrupt:
        exit_code = EXIT_INTERRUPTED
    except Exception:
        log.exception("the crawl stopped with an unexpected error")
        exit_code = EXIT_INTERNAL
    _report_summary(reporter, stats, time.monotonic() - started)
    return exit_code


def _banner_enabled(args: argparse.Namespace, stderr_isatty: bool) -> bool:
    return not args.quiet and (stderr_isatty or args.verbose >= 1)


def _progress_enabled(args: argparse.Namespace, stderr_isatty: bool) -> bool:
    return stderr_isatty and args.verbose == 0 and not args.quiet


async def _crawl(
    seed: str,
    config: CrawlConfig,
    stats: CrawlStats,
    reporter: Reporter,
    *,
    progress: bool,
) -> int:
    async with build_client(config) as client:
        crawler = Crawler(
            Fetcher(client, max_bytes=config.max_bytes),
            reporter,
            config,
            stats,
            robots_loader=partial(load_robots, client, user_agent=config.user_agent),
        )
        task = asyncio.create_task(crawler.run(seed))
        _cancel_on_signal(task)
        progress_line = (
            ProgressLine(sys.stderr, stats, lambda: crawler.pending) if progress else None
        )
        progress_task = asyncio.create_task(progress_line.run()) if progress_line else None
        try:
            outcome = await task
        except asyncio.CancelledError:
            if progress_line is not None:
                progress_line.clear()
            log.warning("interrupted; reporting the pages crawled so far")
            return EXIT_INTERRUPTED
        finally:
            if progress_task is not None:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task
        if outcome.aborted:
            return EXIT_ABORTED
        return EXIT_OK


def _cancel_on_signal(task: asyncio.Task[CrawlOutcome]) -> None:
    loop = asyncio.get_running_loop()
    for number in CANCEL_SIGNALS:
        loop.add_signal_handler(number, task.cancel)


def _report_summary(reporter: Reporter, stats: CrawlStats, elapsed_seconds: float) -> None:
    try:
        reporter.finish(stats, elapsed_seconds)
    except BrokenPipeError:
        _discard_stdout()
    failed = sum(stats.pages_failed.values())
    rate = stats.pages_total / elapsed_seconds if elapsed_seconds > 0 else 0.0
    print(
        f"Crawled {stats.pages_total} pages ({stats.pages_ok} ok, {failed} failed) "
        f"and found {stats.links_found} links in {elapsed_seconds:.1f}s ({rate:.1f} pages/s); "
        f"{stats.retries} retries, {stats.duplicates_dropped} duplicate URLs skipped",
        file=sys.stderr,
    )


def _discard_stdout() -> None:
    """Point stdout at /dev/null so a closed pipe (`| head`) cannot break the rest of the run."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="url-crawler",
        description="Crawl one host and print every page with the links found on it.",
    )
    parser.add_argument("url", help="seed URL; a missing scheme defaults to https://")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULTS.concurrency,
        help="number of worker tasks (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULTS.timeout,
        help="per-request read and write timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULTS.max_pages,
        help="stop after this many pages (default: unlimited)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULTS.max_bytes,
        help="skip a page whose body exceeds this size (default: %(default)s)",
    )
    parser.add_argument("--format", choices=("text", "jsonl"), default="text", help="output format")
    parser.add_argument(
        "--ignore-robots", action="store_true", help="crawl paths that robots.txt disallows"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the start banner and the progress line"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log at INFO, or DEBUG when repeated",
    )
    parser.add_argument("--version", action="version", version=f"url-crawler {__version__}")
    return parser
