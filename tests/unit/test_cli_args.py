from __future__ import annotations

import argparse

import pytest

from url_crawler import __version__
from url_crawler.cli import (
    _banner_enabled,
    _progress_enabled,
    build_parser,
    main,
    prepare_seed,
)


def parse(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def test_defaults() -> None:
    args = parse(["http://example.com"])
    assert args.url == "http://example.com"
    assert args.concurrency == 10
    assert args.timeout == 10.0
    assert args.max_pages is None
    assert args.max_bytes == 5_000_000
    assert args.format == "text"
    assert args.ignore_robots is False
    assert args.quiet is False
    assert args.verbose == 0


def test_flags_are_parsed() -> None:
    args = parse(
        [
            "http://example.com",
            "--concurrency",
            "3",
            "--timeout",
            "2.5",
            "--max-pages",
            "7",
            "--max-bytes",
            "1024",
            "--format",
            "jsonl",
            "--ignore-robots",
            "--quiet",
            "-v",
        ]
    )
    assert (args.concurrency, args.timeout, args.max_pages, args.max_bytes) == (3, 2.5, 7, 1024)
    assert args.format == "jsonl"
    assert args.ignore_robots is True
    assert args.quiet is True
    assert args.verbose == 1


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("example.com", "https://example.com"),
        ("example.com/docs/", "https://example.com/docs/"),
        ("http://example.com", "http://example.com"),
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
        ("HTTPS://example.com", "HTTPS://example.com"),
    ],
)
def test_prepare_seed(typed: str, expected: str) -> None:
    assert prepare_seed(typed) == expected


@pytest.mark.parametrize("typed", ["ftp://example.com", "file:///etc/passwd"])
def test_prepare_seed_rejects_other_schemes(typed: str) -> None:
    with pytest.raises(ValueError, match="unsupported URL scheme"):
        prepare_seed(typed)


@pytest.mark.parametrize(
    "argv",
    [
        ["http://example.com", "--format", "yaml"],
        [],
    ],
)
def test_argparse_usage_errors_exit_with_code_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse(argv)
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["ftp://example.com"],
        ["http://example.com", "--concurrency", "0"],
        ["http://example.com", "--timeout", "0"],
        ["http://example.com", "--max-pages", "0"],
        ["http://example.com", "--max-bytes", "0"],
    ],
)
def test_invalid_values_exit_with_code_2_before_any_request(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(argv)
    assert exit_info.value.code == 2


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "stderr_isatty", "expected"),
    [
        (["http://example.com"], True, True),
        (["http://example.com"], False, False),
        (["http://example.com", "-v"], True, False),
        (["http://example.com", "--quiet"], True, False),
    ],
)
def test_progress_enabled(argv: list[str], stderr_isatty: bool, expected: bool) -> None:
    assert _progress_enabled(parse(argv), stderr_isatty) is expected


@pytest.mark.parametrize(
    ("argv", "stderr_isatty", "expected"),
    [
        (["http://example.com"], True, True),
        (["http://example.com"], False, False),
        (["http://example.com", "-v"], False, True),
        (["http://example.com", "--quiet", "-v"], True, False),
    ],
)
def test_banner_enabled(argv: list[str], stderr_isatty: bool, expected: bool) -> None:
    assert _banner_enabled(parse(argv), stderr_isatty) is expected
