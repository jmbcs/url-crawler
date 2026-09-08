from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import IO, Any

import pytest

from tests.fakesite.server import base_url, serve
from tests.fakesite.site import FakeSite

CLI = [sys.executable, "-m", "url_crawler"]
LOG_PREFIXES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
SLOW_SITE_DELAY_MS = 200
SUMMARY_PATTERN = re.compile(
    r"Crawled \d+ pages \(\d+ ok, \d+ failed\) and found \d+ links in \d+\.\d+s "
    r"\(\d+\.\d+ pages/s\); \d+ retries, \d+ duplicate URLs skipped"
)


@contextlib.contextmanager
def running_site(delay_ms: int = 0) -> Iterator[str]:
    server = serve(FakeSite(), delay_ms=delay_ms)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base_url(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def site_url() -> Iterator[str]:
    with running_site() as url:
        yield url


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*CLI, *args], capture_output=True, text=True, timeout=25, check=False)


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def parse_jsonl(stdout: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = [json.loads(line) for line in stdout.splitlines()]
    pages = [record for record in records if "summary" not in record]
    return pages, records[-1]["summary"]


def read_page_block(stream: IO[str]) -> str:
    """Read lines up to and including the blank line that closes one reported page."""
    lines = []
    while True:
        line = stream.readline()
        lines.append(line)
        if line in ("\n", ""):
            return "".join(lines)


def test_help_lists_the_flags() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    flags = (
        "--concurrency",
        "--timeout",
        "--max-pages",
        "--max-bytes",
        "--format",
        "--ignore-robots",
        "-v",
        "--version",
    )
    for flag in flags:
        assert flag in result.stdout


def test_crawl_prints_pages_and_indented_links(site_url: str) -> None:
    result = run_cli(site_url)
    assert result.returncode == 0
    assert f"{site_url}/\n" in result.stdout
    assert f"  {site_url}/a\n" in result.stdout
    assert f"  {site_url}/leaf\n" in result.stdout
    assert f"{site_url}/missing  [error: http_status 404]\n" in result.stdout


def test_stdout_carries_results_only(site_url: str) -> None:
    result = run_cli(site_url, "-vv")
    assert [line for line in result.stdout.splitlines() if line.startswith(LOG_PREFIXES)] == []
    assert "Crawled " not in result.stdout
    assert "DEBUG url_crawler" in result.stderr


def test_summary_goes_to_stderr(site_url: str) -> None:
    result = run_cli(site_url)
    assert SUMMARY_PATTERN.search(result.stderr) is not None


def test_default_verbosity_logs_nothing(site_url: str) -> None:
    result = run_cli(site_url)
    logged = [line for line in result.stderr.splitlines() if line.startswith(LOG_PREFIXES)]
    assert logged == []


def test_off_scope_links_are_printed_but_not_crawled(site_url: str) -> None:
    pages, _ = parse_jsonl(run_cli(site_url, "--format", "jsonl").stdout)
    crawled = {page["url"] for page in pages}
    printed = {link for page in pages for link in page["links"]}
    assert {"http://external.test/x", f"{site_url}/robots-blocked"} <= printed
    assert crawled.isdisjoint({"http://external.test/x", f"{site_url}/robots-blocked"})


def test_jsonl_output_ends_with_a_summary(site_url: str) -> None:
    result = run_cli(site_url, "--format", "jsonl")
    assert result.returncode == 0
    pages, summary = parse_jsonl(result.stdout)
    assert all("links" in page for page in pages)
    assert summary["pages_ok"] > 0
    assert summary["links_found"] > 0
    assert summary["elapsed_seconds"] > 0


def test_max_pages_stops_the_crawl(site_url: str) -> None:
    result = run_cli(site_url, "--max-pages", "2", "--format", "jsonl")
    assert result.returncode == 0
    pages, summary = parse_jsonl(result.stdout)
    assert len(pages) == 2
    assert summary["pages_ok"] + sum(summary["pages_failed"].values()) == 2
    assert "stopped at --max-pages 2" in result.stderr


def test_ignore_robots_crawls_the_blocked_path(site_url: str) -> None:
    pages, _ = parse_jsonl(run_cli(site_url, "--ignore-robots", "--format", "jsonl").stdout)
    crawled = {page["url"] for page in pages}
    assert f"{site_url}/robots-blocked" in crawled
    assert f"{site_url}/robots-blocked-child" in crawled


def test_unsupported_seed_scheme_exits_2() -> None:
    result = run_cli("ftp://example.com")
    assert result.returncode == 2
    assert "unsupported URL scheme" in result.stderr


def test_missing_scheme_defaults_to_https_and_says_so() -> None:
    port = closed_port()
    result = run_cli(f"127.0.0.1:{port}", "-v")
    assert result.returncode == 3
    assert f"assuming https:// for 127.0.0.1:{port}" in result.stderr


def test_unreachable_seed_exits_3() -> None:
    started = time.monotonic()
    result = run_cli(f"http://127.0.0.1:{closed_port()}", "--timeout", "1")
    assert result.returncode == 3
    assert time.monotonic() - started < 20
    assert "Could not fetch seed" in result.stderr
    assert "Crawled 0 pages" in result.stderr


def test_sigint_stops_the_crawl_after_a_complete_page() -> None:
    with running_site(delay_ms=SLOW_SITE_DELAY_MS) as site_url:
        process = subprocess.Popen(
            [*CLI, site_url, "--concurrency", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        seed_block = read_page_block(process.stdout)
        process.send_signal(signal.SIGINT)
        stdout = seed_block + process.stdout.read()
        stderr = process.stderr.read()
        process.wait(timeout=20)
    assert process.returncode == 130
    assert stdout.startswith(f"{site_url}/\n")
    assert stdout.endswith("\n\n")
    assert "Crawled " in stderr


def test_closed_stdout_exits_0_without_a_traceback() -> None:
    with running_site(delay_ms=SLOW_SITE_DELAY_MS) as site_url:
        process = subprocess.Popen(
            [*CLI, site_url, "--concurrency", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        os.read(process.stdout.fileno(), 1)
        process.stdout.close()
        stderr = process.stderr.read()
        process.wait(timeout=20)
    assert process.returncode == 0
    assert "Traceback" not in stderr
    assert "Crawled " in stderr
