from __future__ import annotations

import subprocess
import sys

import pytest

LOG_PREFIXES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@pytest.mark.network
@pytest.mark.timeout(90)
def test_cli_crawls_live_site() -> None:
    # crawler-test.com ships a deliberate `Disallow: //` line that stdlib
    # robotparser reads as block-all; disable robots checks to exercise default config.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "url_crawler",
            "https://crawler-test.com",
            "--max-pages",
            "5",
            "--timeout",
            "15",
            "--ignore-robots",
        ],
        capture_output=True,
        text=True,
        timeout=80,
    )

    assert result.returncode == 0, f"exit code {result.returncode}, stderr: {result.stderr}"

    page_lines = [line for line in result.stdout.splitlines() if line and not line.startswith("  ")]
    assert len(page_lines) >= 1, f"expected at least one page line, got: {result.stdout}"
    assert page_lines[0].startswith("https://crawler-test.com/"), (
        f"first page should be seed, got: {page_lines[0]}"
    )

    # Verify stdout carries only results, not logs
    logged_lines = [
        line for line in result.stdout.splitlines() if line and line.startswith(LOG_PREFIXES)
    ]
    assert logged_lines == [], f"stdout should not contain log lines, got: {logged_lines}"

    # Verify summary is on stderr
    assert "Crawled " in result.stderr, f"stderr should contain summary, got: {result.stderr}"
