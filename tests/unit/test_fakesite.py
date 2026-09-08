from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import httpx

from tests.fakesite.app import asgi_app
from tests.fakesite.server import base_url, serve
from tests.fakesite.site import EXPECTED_CRAWLED, NEVER_REQUESTED, FakeSite


@contextmanager
def running_server(site: FakeSite) -> Iterator[ThreadingHTTPServer]:
    server = serve(site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_root_is_html(fake_client: httpx.AsyncClient) -> None:
    response = await fake_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-length"] == str(len(response.content))
    assert '<a href="/a">' in response.text


async def test_flaky_fails_once_then_succeeds(fake_client: httpx.AsyncClient) -> None:
    first = await fake_client.get("/flaky")
    second = await fake_client.get("/flaky")
    assert first.status_code == 500
    assert second.status_code == 200
    assert "/flaky-child" in second.text


async def test_unknown_path_is_404(fake_client: httpx.AsyncClient) -> None:
    response = await fake_client.get("/missing")
    assert response.status_code == 404


async def test_pdf_is_served_as_pdf(fake_client: httpx.AsyncClient) -> None:
    response = await fake_client.get("/file.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


async def test_redirect_carries_location(fake_client: httpx.AsyncClient) -> None:
    response = await fake_client.get("/redirect")
    assert response.status_code == 301
    assert response.headers["location"] == "/redirected"


async def test_query_string_is_ignored_and_requests_are_recorded(
    fake_site: FakeSite, fake_client: httpx.AsyncClient
) -> None:
    response = await fake_client.get("/leaf?utm_source=x")
    assert response.status_code == 200
    assert fake_site.requested == ["/leaf"]


async def test_off_host_request_is_recorded_as_an_absolute_url(
    fake_site: FakeSite, fake_client: httpx.AsyncClient
) -> None:
    await fake_client.get("http://external.test/landing")
    assert fake_site.requested == ["http://external.test/landing"]


def test_crawl_contract_sets_are_consistent() -> None:
    pages = FakeSite().pages
    assert not EXPECTED_CRAWLED & NEVER_REQUESTED
    assert EXPECTED_CRAWLED - pages.keys() == {"/missing", "/deep/x"}
    assert {path for path in NEVER_REQUESTED if path.startswith("/")} <= pages.keys()


async def test_generated_site_links_stay_inside_itself() -> None:
    site = FakeSite.generated(pages=4, links_per_page=2)
    transport = httpx.ASGITransport(app=asgi_app(site))
    async with httpx.AsyncClient(transport=transport, base_url="http://site.test") as client:
        response = await client.get("/p/3")
    assert response.status_code == 200
    assert '<a href="/p/0">' in response.text
    assert '<a href="/p/1">' in response.text


def test_serve_answers_real_gets_over_one_keep_alive_connection() -> None:
    site = FakeSite()
    with running_server(site) as server, httpx.Client(timeout=5.0) as client:
        root = client.get(f"{base_url(server)}/")
        leaf = client.get(f"{base_url(server)}/leaf?x=1")
    assert root.status_code == 200
    assert root.http_version == "HTTP/1.1"
    assert '<a href="/leaf">' in root.text
    assert leaf.headers["content-length"] == str(len(leaf.content))
    assert site.requested == ["/", "/leaf"]


def test_serve_sends_redirect_status_and_location() -> None:
    site = FakeSite()
    with running_server(site) as server, httpx.Client(timeout=5.0) as client:
        response = client.get(f"{base_url(server)}/redirect")
    assert response.status_code == 301
    assert response.headers["location"] == "/redirected"
    assert site.requested == ["/redirect"]


async def test_lifespan_scope_is_ignored(fake_site: FakeSite) -> None:
    async def receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    sent: list[object] = []

    async def send(message: object) -> None:
        sent.append(message)

    await asgi_app(fake_site)({"type": "lifespan"}, receive, send)
    assert sent == []
    assert fake_site.requested == []
