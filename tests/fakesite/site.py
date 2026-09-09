"""Deterministic fake site shared by the integration tests, the CLI test and the benchmark."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Page:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)


# /robots.txt is fetched but never reported as a page; /flaky is requested twice, reported once.
EXPECTED_CRAWLED = frozenset(
    {
        "/",
        "/a",
        "/b",
        "/loop",
        "/missing",
        "/flaky",
        "/flaky-child",
        "/redirect",
        "/redirected",
        "/redirect-cycle-1",
        "/redirect-cycle-2",
        "/off-site-redirect",
        "/file.pdf",
        "/leaf",
        "/malformed",
        "/malformed-child",
        "/base",
        "/deep/x",
        "/nofollow",
        "/nofollow-target",
    }
)

NEVER_REQUESTED = frozenset(
    {
        "/robots-blocked",
        "/robots-blocked-child",
        "http://external.test/x",
        "http://external.test/landing",
        "http://sub.site.test/x",
    }
)

_NOT_FOUND = Page(status=404, body=b"<html><body>not found</body></html>")


def _html(body: str) -> Page:
    return Page(body=f"<html><body>{body}</body></html>".encode())


def _links(*targets: str) -> Page:
    return _html("".join(f'<a href="{target}">{target}</a>' for target in targets))


def _redirect(status: int, location: str) -> Page:
    return Page(status=status, headers={"location": location})


def _hazard_pages(host: str) -> dict[str, Page]:
    return {
        "/": _links(
            "/a",
            "/b",
            "/loop",
            "/missing",
            "/flaky",
            "/redirect",
            "/redirect-cycle-1",
            "/off-site-redirect",
            "/file.pdf",
            "/leaf",
            "/malformed",
            "/base",
            "/nofollow",
            "/robots-blocked",
            "http://external.test/x",
            f"http://sub.{host}/x",
            "mailto:hello@example.com",
            "javascript:void(0)",
            "#top",
        ),
        "/a": _links("/b", "/"),
        "/b": _links("/a"),
        "/loop": _links("/loop"),
        "/flaky": _links("/flaky-child"),
        "/flaky-child": _html("<p>flaky child</p>"),
        "/redirect": _redirect(301, "/redirected"),
        "/redirected": _html("<p>redirected</p>"),
        "/redirect-cycle-1": _redirect(302, "/redirect-cycle-2"),
        "/redirect-cycle-2": _redirect(302, "/redirect-cycle-1"),
        "/off-site-redirect": _redirect(302, "http://external.test/landing"),
        "/file.pdf": Page(body=b"%PDF-1.4\n", content_type="application/pdf"),
        "/leaf": _html("<p>no links here</p>"),
        "/malformed": _html(
            '<a href="/a">first<div><a href="/malformed-child">second</div><p>unclosed'
        ),
        "/malformed-child": _html("<p>malformed child</p>"),
        "/base": _html('<base href="/deep/"><a href="x">x</a>'),
        "/nofollow": _html('<a href="/nofollow-target" rel="nofollow">target</a>'),
        "/nofollow-target": _html("<p>nofollow target</p>"),
        "/robots-blocked": _links("/robots-blocked-child"),
        "/robots-blocked-child": _html("<p>blocked child</p>"),
        "/robots.txt": Page(
            body=b"User-agent: *\nDisallow: /robots-blocked\n",
            content_type="text/plain; charset=utf-8",
        ),
    }


def _generated_pages(count: int, links_per_page: int) -> dict[str, Page]:
    span = min(links_per_page, max(count - 1, 0))
    pages = {"/": _links("/p/0")}
    for index in range(count):
        targets = [f"/p/{(index + offset) % count}" for offset in range(1, span + 1)]
        pages[f"/p/{index}"] = _links(*targets)
    return pages


class FakeSite:
    """Routes paths to canned responses; /flaky fails once before succeeding."""

    def __init__(self, host: str = "site.test", *, not_found: Page = _NOT_FOUND) -> None:
        self.host = host
        self.requested: list[str] = []
        self.pages = _hazard_pages(host)
        self._flaky_requests = 0
        self._not_found = not_found

    @classmethod
    def generated(cls, pages: int, links_per_page: int = 5, host: str = "site.test") -> FakeSite:
        site = cls(host)
        site.pages = _generated_pages(pages, links_per_page)
        return site

    @classmethod
    def failing(cls, host: str = "site.test", status: int = 503, children: int = 30) -> FakeSite:
        """Home page links to `children` distinct paths that all answer with `status`."""
        site = cls(host, not_found=Page(status=status))
        site.pages = {"/": _links(*(f"/e/{index}" for index in range(children)))}
        return site

    def respond(self, path: str, host: str | None = None) -> Page:
        path = path.split("?", 1)[0]
        self.requested.append(path if host in (None, self.host) else f"http://{host}{path}")
        if path == "/flaky":
            self._flaky_requests += 1
            if self._flaky_requests == 1:
                return Page(status=500, body=b"<html><body>server error</body></html>")
        return self.pages.get(path, self._not_found)
