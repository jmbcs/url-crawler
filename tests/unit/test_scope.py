import pytest

from url_crawler.urls import HostScope

SEED = "https://example.com/"


@pytest.fixture
def scope() -> HostScope:
    return HostScope.from_url(SEED)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://example.com/about",
        "https://example.com/deep/page?q=1",
        "https://EXAMPLE.com/about",
        "HTTPS://EXAMPLE.COM/about",
        "https://example.com:443/",
        "http://example.com/insecure",
        "https://user@example.com/a",
    ],
)
def test_scope_allows(scope: HostScope, url: str) -> None:
    assert scope.allows(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://blog.example.com/",
        "https://www.example.com/",
        "https://notexample.com/",
        "https://example.com.evil.tld/",
        "https://example.com:8080/",
        "http://example.com:8080/",
        "https://sub.sub.example.com/",
        "https://other.test/",
    ],
)
def test_scope_excludes(scope: HostScope, url: str) -> None:
    assert scope.allows(url) is False


@pytest.mark.parametrize(
    "url",
    ["mailto:someone@example.com", "javascript:void(0)", "not a url", "", "/relative"],
)
def test_scope_rejects_uncrawlable(scope: HostScope, url: str) -> None:
    assert scope.allows(url) is False


def test_from_url_reads_host_and_default_port() -> None:
    assert HostScope.from_url(SEED) == HostScope(host="example.com", port=None)


def test_from_url_keeps_explicit_non_default_port() -> None:
    assert HostScope.from_url("http://example.com:8080/") == HostScope(
        host="example.com", port=8080
    )


def test_explicit_port_scope_allows_only_that_port() -> None:
    scope = HostScope.from_url("http://example.com:8080/")
    assert scope.allows("http://example.com:8080/a") is True
    assert scope.allows("http://example.com/a") is False


def test_scope_re_anchored_to_www_excludes_apex() -> None:
    scope = HostScope.from_url("https://www.example.com/")
    assert scope.allows("https://www.example.com/a") is True
    assert scope.allows("https://example.com/a") is False
