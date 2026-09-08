from __future__ import annotations

import random
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest

from url_crawler.fetcher import Fetcher
from url_crawler.models import FetchError, FetchErrorKind, FetchResult

Handler = Callable[[httpx.Request], httpx.Response]

NOW = datetime(2026, 1, 1, tzinfo=UTC)
URL = "https://example.com/"
HTML = b"<html><body><a href='/a'>a</a></body></html>"


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def fetch_with(
    handler: Handler,
    url: str = URL,
    *,
    max_bytes: int = 1_000_000,
    max_attempts: int = 3,
) -> tuple[FetchResult | FetchError, list[float]]:
    sleep = RecordingSleep()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        fetcher = Fetcher(
            client,
            max_bytes=max_bytes,
            max_attempts=max_attempts,
            sleep=sleep,
            rng=random.Random(0),
            now=lambda: NOW,
        )
        outcome = await fetcher.fetch(url)
    return outcome, sleep.delays


def responder(*responses: httpx.Response) -> tuple[Handler, list[httpx.Request]]:
    """Handler replying with each response in turn, repeating the last one."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses[min(len(requests) - 1, len(responses) - 1)]

    return handler, requests


def raiser(exc: Exception) -> tuple[Handler, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise exc

    return handler, requests


async def test_fetches_html_body() -> None:
    handler, requests = responder(
        httpx.Response(200, content=HTML, headers={"content-type": "text/html; charset=utf-8"})
    )

    result, delays = await fetch_with(handler)

    assert result == FetchResult(URL, 200, HTML, "text/html", None, 1)
    assert len(requests) == 1
    assert delays == []


async def test_retries_server_error_then_succeeds() -> None:
    handler, requests = responder(
        httpx.Response(500),
        httpx.Response(200, content=HTML, headers={"content-type": "text/html"}),
    )

    result, delays = await fetch_with(handler)

    assert isinstance(result, FetchResult)
    assert result.status == 200
    assert result.attempts == 2
    assert len(requests) == 2
    assert len(delays) == 1
    assert 0.0 <= delays[0] <= 1.0


async def test_does_not_retry_client_error() -> None:
    handler, requests = responder(httpx.Response(404))

    result, delays = await fetch_with(handler)

    assert result == FetchError(URL, FetchErrorKind.HTTP_STATUS, 404, "HTTP 404", 1)
    assert len(requests) == 1
    assert delays == []


async def test_honours_retry_after_on_429() -> None:
    handler, requests = responder(httpx.Response(429, headers={"retry-after": "3"}))

    result, delays = await fetch_with(handler)

    assert isinstance(result, FetchError)
    assert result.status == 429
    assert result.attempts == 3
    assert len(requests) == 3
    assert delays == [3.0, 3.0]


async def test_timeout_exhausts_attempts() -> None:
    handler, requests = raiser(httpx.ReadTimeout("read timed out"))

    result, delays = await fetch_with(handler)

    assert result == FetchError(URL, FetchErrorKind.TIMEOUT, None, "read timed out", 3)
    assert len(requests) == 3
    assert len(delays) == 2


async def test_connect_error_reports_connection() -> None:
    handler, _ = raiser(httpx.ConnectError("no route to host"))

    result, delays = await fetch_with(handler)

    assert result == FetchError(URL, FetchErrorKind.CONNECTION, None, "no route to host", 3)
    assert len(delays) == 2


async def test_remote_protocol_error_is_retried() -> None:
    handler, requests = raiser(httpx.RemoteProtocolError("server disconnected"))

    result, _ = await fetch_with(handler)

    assert isinstance(result, FetchError)
    assert result.kind is FetchErrorKind.PROTOCOL
    assert len(requests) == 3


async def test_local_protocol_error_is_not_retried() -> None:
    handler, requests = raiser(httpx.LocalProtocolError("illegal header"))

    result, delays = await fetch_with(handler)

    assert result == FetchError(URL, FetchErrorKind.PROTOCOL, None, "illegal header", 1)
    assert len(requests) == 1
    assert delays == []


@pytest.mark.parametrize(
    "exc",
    [httpx.UnsupportedProtocol("unsupported scheme"), httpx.InvalidURL("no host")],
)
async def test_unsupported_url_is_not_retried(exc: Exception) -> None:
    handler, requests = raiser(exc)

    result, delays = await fetch_with(handler)

    assert isinstance(result, FetchError)
    assert result.kind is FetchErrorKind.INVALID_URL
    assert result.attempts == 1
    assert len(requests) == 1
    assert delays == []


async def test_exception_without_message_falls_back_to_type_name() -> None:
    handler, _ = raiser(httpx.LocalProtocolError(""))

    result, _ = await fetch_with(handler)

    assert isinstance(result, FetchError)
    assert result.message == "LocalProtocolError"


async def test_rejects_non_html_without_reading_body() -> None:
    consumed = False

    async def body() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield b"%PDF-1.7"

    handler, _ = responder(
        httpx.Response(200, headers={"content-type": "application/pdf"}, content=body())
    )

    result, _ = await fetch_with(handler)

    assert result == FetchError(URL, FetchErrorKind.UNSUPPORTED_CONTENT, 200, "application/pdf", 1)
    assert consumed is False


async def test_missing_content_type_is_treated_as_html() -> None:
    handler, _ = responder(httpx.Response(200, content=HTML))

    result, _ = await fetch_with(handler)

    assert isinstance(result, FetchResult)
    assert result.content_type == "text/html"
    assert result.body == HTML


async def test_charset_and_case_are_stripped_from_content_type() -> None:
    handler, _ = responder(
        httpx.Response(200, content=HTML, headers={"content-type": "TEXT/HTML ; charset=latin-1"})
    )

    result, _ = await fetch_with(handler)

    assert isinstance(result, FetchResult)
    assert result.content_type == "text/html"


async def test_declared_content_length_over_limit_is_rejected_without_reading() -> None:
    consumed = False

    async def body() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield b"x" * 200

    handler, _ = responder(
        httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "200"},
            content=body(),
        )
    )

    result, _ = await fetch_with(handler, max_bytes=100)

    assert isinstance(result, FetchError)
    assert result.kind is FetchErrorKind.TOO_LARGE
    assert result.status == 200
    assert result.attempts == 1
    assert consumed is False


async def test_streamed_body_over_limit_is_rejected() -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b"x" * 40

    handler, _ = responder(
        httpx.Response(200, headers={"content-type": "text/html"}, content=body())
    )

    result, _ = await fetch_with(handler, max_bytes=100)

    assert isinstance(result, FetchError)
    assert result.kind is FetchErrorKind.TOO_LARGE


async def test_body_exactly_at_limit_is_accepted() -> None:
    handler, _ = responder(
        httpx.Response(200, content=b"x" * 100, headers={"content-type": "text/html"})
    )

    result, _ = await fetch_with(handler, max_bytes=100)

    assert isinstance(result, FetchResult)
    assert result.body == b"x" * 100


async def test_redirect_returns_location_and_empty_body() -> None:
    handler, _ = responder(
        httpx.Response(
            302,
            headers={"location": "/next", "content-type": "text/html; charset=utf-8"},
            content=HTML,
        )
    )

    result, delays = await fetch_with(handler)

    assert result == FetchResult(URL, 302, b"", "text/html", "/next", 1)
    assert delays == []


async def test_non_redirect_3xx_is_not_a_redirect() -> None:
    handler, _ = responder(httpx.Response(304, headers={"location": "/x"}))

    result, _ = await fetch_with(handler)

    assert isinstance(result, FetchResult)
    assert result.status == 304
    assert result.location is None


async def test_mid_body_protocol_error_is_retried() -> None:
    async def broken_body() -> AsyncIterator[bytes]:
        yield b"<html>"
        raise httpx.RemoteProtocolError("peer closed")

    handler, requests = responder(
        httpx.Response(200, headers={"content-type": "text/html"}, content=broken_body()),
        httpx.Response(200, content=HTML, headers={"content-type": "text/html"}),
    )

    result, delays = await fetch_with(handler)

    assert isinstance(result, FetchResult)
    assert result.body == HTML
    assert result.attempts == 2
    assert len(requests) == 2
    assert len(delays) == 1


async def test_empty_mime_type_is_treated_as_html() -> None:
    handler, _ = responder(
        httpx.Response(200, content=HTML, headers={"content-type": "; charset=utf-8"})
    )

    result, _ = await fetch_with(handler)

    assert isinstance(result, FetchResult)
    assert result.content_type == "text/html"
    assert result.body == HTML
