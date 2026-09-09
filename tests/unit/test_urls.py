import pytest

from url_crawler.urls import (
    MAX_URL_LENGTH,
    canonical_key,
    normalize,
    prepare_seed,
    resolve_href,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTP://EXAMPLE.com/Path", "http://example.com/Path"),
        ("HTTPS://Example.COM/", "https://example.com/"),
        ("http://example.com", "http://example.com/"),
        ("http://example.com?q=1", "http://example.com/?q=1"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:8080/a", "http://example.com:8080/a"),
        ("https://example.com:80/a", "https://example.com:80/a"),
        ("http://example.com/a#section-2", "http://example.com/a"),
        ("http://example.com/#", "http://example.com/"),
        ("http://example.com/dir/", "http://example.com/dir/"),
        ("http://example.com/dir", "http://example.com/dir"),
        ("  http://example.com/a  ", "http://example.com/a"),
        ("\n\thttps://example.com/a\n", "https://example.com/a"),
        ("http://example.com/?b=2&a=1", "http://example.com/?b=2&a=1"),
        ("http://example.com/?utm_source=x&ref=y", "http://example.com/?utm_source=x&ref=y"),
        ("http://example.com/?empty=", "http://example.com/?empty="),
        ("http://User:Pass@example.com/a", "http://example.com/a"),
        ("http://user@example.com:80/", "http://example.com/"),
        ("http://user:pass@example.com:8080/a", "http://example.com:8080/a"),
        ("http://example.com/a%20b", "http://example.com/a%20b"),
        ("http://example.com/Caf%C3%A9", "http://example.com/Caf%C3%A9"),
        ("http://[::1]:8080/x", "http://[::1]:8080/x"),
        ("http://[::1]:80/x", "http://[::1]/x"),
        ("http://example.com/café", "http://example.com/café"),
        ("http://example.com/a/./b", "http://example.com/a/b"),
        ("http://example.com/a/../b", "http://example.com/b"),
        ("http://example.com/a/b/..", "http://example.com/a/"),
        ("http://example.com/a/b/.", "http://example.com/a/b/"),
        ("http://example.com/a/b/../..", "http://example.com/"),
        ("http://example.com/../../x", "http://example.com/x"),
        ("http://example.com/..", "http://example.com/"),
        ("http://example.com/.", "http://example.com/"),
        ("http://example.com/./", "http://example.com/"),
        ("http://example.com/a/..?q=1", "http://example.com/?q=1"),
        ("http://example.com/a//b", "http://example.com/a//b"),
        ("http://example.com/a/./b/../c/", "http://example.com/a/c/"),
    ],
)
def test_normalize_canonical_form(url: str, expected: str) -> None:
    assert normalize(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "mailto:someone@example.com",
        "tel:+15550100",
        "javascript:void(0)",
        "JavaScript:alert(1)",
        "data:text/html,<b>x</b>",
        "ftp://example.com/file.txt",
        "file:///etc/hosts",
        "http:///only-a-path",
        "http://",
        "http://exa mple.com/",
        "http:// example.com/",
        "/relative/path",
        "example.com",
        "",
        "   ",
        "http://example.com:port/",
        "http://[::1/",
    ],
)
def test_normalize_rejects(url: str) -> None:
    assert normalize(url) is None


def test_normalize_accepts_url_at_length_limit() -> None:
    prefix = "http://example.com/"
    url = prefix + "a" * (MAX_URL_LENGTH - len(prefix))
    assert normalize(url) == url


def test_normalize_rejects_url_over_length_limit() -> None:
    url = "http://example.com/" + "a" * MAX_URL_LENGTH
    assert normalize(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "HTTP://EXAMPLE.com/Path?b=2&a=1#frag",
        "http://example.com:80/dir/",
        "http://user@example.com/a",
        "http://example.com/a/../b/./c/",
        "http://example.com/café?q=café",
    ],
)
def test_normalize_is_idempotent(url: str) -> None:
    once = normalize(url)
    assert once is not None
    assert normalize(once) == once


BASE = "https://example.com/docs/guide.html"


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("page.html", "https://example.com/docs/page.html"),
        ("./page.html", "https://example.com/docs/page.html"),
        ("/page.html", "https://example.com/page.html"),
        ("../up.html", "https://example.com/up.html"),
        ("../../way/up.html", "https://example.com/way/up.html"),
        ("sub/deep.html", "https://example.com/docs/sub/deep.html"),
        ("//cdn.example.com/x", "https://cdn.example.com/x"),
        ("http://other.test/x", "http://other.test/x"),
        ("  /spaced  ", "https://example.com/spaced"),
        ("\n\t/newline\n", "https://example.com/newline"),
        ("#top", BASE),
        ("", BASE),
        ("?q=1", "https://example.com/docs/guide.html?q=1"),
        ("/other#frag", "https://example.com/other"),
        ("/A/B?z=1", "https://example.com/A/B?z=1"),
    ],
)
def test_resolve_href(href: str, expected: str) -> None:
    assert resolve_href(href, BASE) == expected


@pytest.mark.parametrize(
    "href",
    ["mailto:someone@example.com", "javascript:void(0)", "tel:+15550100", "data:,x", "ftp://a/b"],
)
def test_resolve_href_rejects(href: str) -> None:
    assert resolve_href(href, BASE) is None


def test_resolve_href_against_protocol_relative_base_keeps_http() -> None:
    assert resolve_href("//cdn.example.com/x", "http://example.com/") == "http://cdn.example.com/x"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/?b=2&a=1", "https://example.com/?a=1&b=2"),
        ("https://example.com/?a=1&b=2", "https://example.com/?a=1&b=2"),
        ("https://example.com/?z=1&z=0", "https://example.com/?z=1&z=0"),
        ("https://example.com/?b=1&a=2", "https://example.com/?a=2&b=1"),
        ("https://example.com/?a=2&b=1", "https://example.com/?a=2&b=1"),
        ("https://example.com/?a=2&a=1", "https://example.com/?a=2&a=1"),
        ("https://example.com/%7Euser", "https://example.com/~user"),
        ("https://example.com/a%2db", "https://example.com/a-b"),
        ("https://example.com/a%2Eb", "https://example.com/a.b"),
        ("https://example.com/a%5fb", "https://example.com/a_b"),
        ("https://example.com/a%41b", "https://example.com/aAb"),
        ("https://example.com/a%3fb", "https://example.com/a%3Fb"),
        ("https://example.com/a%20b", "https://example.com/a%20b"),
        ("https://example.com/?q=%7evalue", "https://example.com/?q=~value"),
        ("https://example.com/?q=%26raw", "https://example.com/?q=%26raw"),
        ("https://example.com/café", "https://example.com/caf%C3%A9"),
        ("https://example.com/caf%c3%a9", "https://example.com/caf%C3%A9"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/a/b", "https://example.com/a/b"),
        ("https://example.com/a?single=1", "https://example.com/a?single=1"),
    ],
)
def test_canonical_key(url: str, expected: str) -> None:
    assert canonical_key(url) == expected


def test_canonical_key_matches_for_reordered_query() -> None:
    assert canonical_key("https://example.com/s?b=2&a=1") == canonical_key(
        "https://example.com/s?a=1&b=2"
    )


def test_canonical_key_matches_for_escaped_non_ascii() -> None:
    assert canonical_key("https://example.com/café") == canonical_key(
        "https://example.com/caf%C3%A9"
    )


def test_canonical_key_keeps_the_order_of_repeated_parameters() -> None:
    assert canonical_key("https://example.com/s?a=2&a=1") != canonical_key(
        "https://example.com/s?a=1&a=2"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://example.com/café?b=2&a=1",
        "https://example.com/a%20b?q=%7evalue",
        "https://example.com/%7Euser?z=1&z=0",
    ],
)
def test_canonical_key_is_idempotent(url: str) -> None:
    once = canonical_key(url)
    assert canonical_key(once) == once


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://example.com/A", "https://example.com/a"),
        ("https://example.com/a", "https://example.com/a/"),
        ("https://example.com/a?x=1", "https://example.com/a?x=2"),
        ("https://example.com/a", "http://example.com/a"),
    ],
)
def test_canonical_key_distinguishes(left: str, right: str) -> None:
    assert canonical_key(left) != canonical_key(right)


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
    "url",
    [
        "http://example.com/a\x1bb",
        "http://example.com/a\x07b",
        "http://example.com/a\x00b",
        "http://example.com/a\nb",
        "http://example.com/a\tb",
        "http://example.com/a\rb",
        "http://example.com/a\x7fb",
        "http://example.com/?q=\x1b[2J",
        "http://example.com/?q=a\x07b",
        "http://example.com/?q=a\x00b",
        "http://example.com/?q=a\tb",
        "http://example.com/?q=a\x7fb",
        "http://exa\x1bmple.com/",
        "http://exa\x07mple.com/",
        "http://exa\x00mple.com/",
        "http://exa\tmple.com/",
        "http://exa\x7fmple.com/",
    ],
)
def test_normalize_rejects_control_characters(url: str) -> None:
    assert normalize(url) is None


@pytest.mark.parametrize("href", ["/a\x1bb", "/a\x00b", "/a\x07b", "/a\x7fb", "/?q=\x1b[2J"])
def test_resolve_href_rejects_control_characters(href: str) -> None:
    assert resolve_href(href, BASE) is None
