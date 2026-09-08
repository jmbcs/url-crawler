from __future__ import annotations

from pathlib import Path

import pytest

from url_crawler.parser import extract_links

FIXTURES = Path(__file__).parent.parent / "fixtures" / "html"
BASE = "https://example.com/dir/page.html"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (
            "simple.html",
            [
                "https://example.com/",
                "https://example.com/dir/guide.html",
                "https://example.com/about",
                "https://other.example.org/spec",
                "https://example.com/dir/page.html",
            ],
        ),
        (
            "base_href.html",
            [
                "https://example.com/deep/x",
                "https://example.com/root",
                "https://example.com/up",
            ],
        ),
        (
            "malformed.html",
            [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
                "https://example.com/d",
                "https://example.com/e",
                "https://example.com/F",
            ],
        ),
        ("no_links.html", []),
        (
            "mixed_schemes.html",
            [
                "http://plain.example.com/insecure",
                "https://example.com/secure",
                "https://cdn.example.com/asset",
                "https://example.com/dir/page.html",
                "https://example.com/padded",
            ],
        ),
        (
            "duplicate_anchors.html",
            [
                "https://example.com/",
                "https://example.com/pricing",
                "https://example.com/contact",
                "https://example.com/pricing?plan=team",
                "https://example.com/PRICING",
            ],
        ),
        (
            "unclosed_anchor.html",
            [
                "https://example.com/before",
                "https://example.com/spanning",
                "https://example.com/after",
            ],
        ),
        (
            "area_map.html",
            [
                "https://example.com/plain",
                "https://example.com/rooms/kitchen",
                "https://example.com/rooms/hall",
            ],
        ),
    ],
)
def test_extracts_expected_links_in_document_order(fixture: str, expected: list[str]) -> None:
    assert extract_links(load(fixture), BASE) == expected


def test_unclosed_anchor_href_appears_exactly_once() -> None:
    links = extract_links(load("unclosed_anchor.html"), BASE)
    assert links.count("https://example.com/spanning") == 1


@pytest.mark.parametrize("body", [b"", b"   ", b"<html><body></body></html>"])
def test_body_without_anchors_yields_no_links(body: bytes) -> None:
    assert extract_links(body, BASE) == []


def test_undecodable_href_drops_only_that_link() -> None:
    body = b'<a href="/caf\xe9">bad</a><a href="/ok" title="caf\xe9">ok</a><a href="/two">two</a>'
    assert extract_links(body, BASE) == ["https://example.com/ok", "https://example.com/two"]


def test_undecodable_base_href_falls_back_to_the_page_url() -> None:
    body = b'<head><base href="/\xe9/"></head><a href="x">x</a>'
    assert extract_links(body, BASE) == ["https://example.com/dir/x"]


def test_latin1_text_does_not_hide_links() -> None:
    body = "<p>café</p><a href='/menu'>menu</a>".encode("latin-1")
    assert extract_links(body, BASE) == ["https://example.com/menu"]


def test_unresolvable_base_href_falls_back_to_the_page_url() -> None:
    body = b'<head><base href="mailto:webmaster@example.com"></head><body><a href="x">x</a>'
    assert extract_links(body, BASE) == ["https://example.com/dir/x"]


def test_only_the_first_base_href_is_used() -> None:
    body = b'<head><base href="/first/"><base href="/second/"></head><body><a href="x">x</a>'
    assert extract_links(body, BASE) == ["https://example.com/first/x"]
