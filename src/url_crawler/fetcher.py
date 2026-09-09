from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from url_crawler.models import FetchError, FetchErrorKind, FetchResult
from url_crawler.retry import retry_delay

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_ERROR_KINDS: tuple[tuple[type[Exception], FetchErrorKind], ...] = (
    (httpx.TimeoutException, FetchErrorKind.TIMEOUT),
    (httpx.ProtocolError, FetchErrorKind.PROTOCOL),
    (httpx.DecodingError, FetchErrorKind.PROTOCOL),
    (httpx.NetworkError, FetchErrorKind.CONNECTION),
    (httpx.UnsupportedProtocol, FetchErrorKind.INVALID_URL),
    (httpx.InvalidURL, FetchErrorKind.INVALID_URL),
)

_HANDLED_EXCEPTIONS: tuple[type[Exception], ...] = tuple(cls for cls, _ in _ERROR_KINDS)


@dataclass(frozen=True, slots=True)
class _Retry:
    delay: float


def _error_kind(exc: Exception) -> FetchErrorKind:
    return next(kind for cls, kind in _ERROR_KINDS if isinstance(exc, cls))


def _mime_type(response: httpx.Response) -> str:
    content_type: str = response.headers.get("content-type", "text/html")
    mime_type = content_type.split(";", 1)[0].strip().lower()
    return mime_type or "text/html"


class Fetcher:
    """Fetches one URL with bounded retries, streaming the body behind content and size gates."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_bytes: int,
        max_attempts: int = 3,
        request_budget: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._max_attempts = max_attempts
        self._request_budget = request_budget
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._now = now

    async def fetch(self, url: str) -> FetchResult | FetchError:
        attempt = 1
        while True:
            outcome = await self._attempt(url, attempt)
            if not isinstance(outcome, _Retry):
                return outcome
            await self._sleep(outcome.delay)
            attempt += 1

    async def _attempt(self, url: str, attempt: int) -> FetchResult | FetchError | _Retry:
        try:
            async with asyncio.timeout(self._request_budget):
                async with self._client.stream("GET", url) as response:
                    return await self._handle(url, response, attempt)
        except TimeoutError:
            # The budget covers the whole request, so report and retry it like any read timeout.
            exc: Exception = httpx.TimeoutException(
                f"request took longer than the {self._request_budget}s budget"
            )
        except _HANDLED_EXCEPTIONS as caught:
            exc = caught
        delay = self._delay(status=None, exc=exc, attempt=attempt, retry_after=None)
        if delay is None:
            message = str(exc) or type(exc).__name__
            return FetchError(url, _error_kind(exc), None, message, attempt)
        return _Retry(delay)

    async def _handle(
        self, url: str, response: httpx.Response, attempt: int
    ) -> FetchResult | FetchError | _Retry:
        status = response.status_code
        mime_type = _mime_type(response)
        if status in REDIRECT_STATUSES:
            location: str | None = response.headers.get("location")
            return FetchResult(url, status, b"", mime_type, location, attempt)
        if status >= 400:
            delay = self._delay(
                status=status,
                exc=None,
                attempt=attempt,
                retry_after=response.headers.get("retry-after"),
            )
            if delay is None:
                return FetchError(
                    url, FetchErrorKind.HTTP_STATUS, status, f"HTTP {status}", attempt
                )
            return _Retry(delay)
        if mime_type not in HTML_CONTENT_TYPES:
            return FetchError(url, FetchErrorKind.UNSUPPORTED_CONTENT, status, mime_type, attempt)
        body = await self._read_capped(response)
        if body is None:
            message = f"body larger than {self._max_bytes} bytes"
            return FetchError(url, FetchErrorKind.TOO_LARGE, status, message, attempt)
        return FetchResult(url, status, body, mime_type, None, attempt)

    async def _read_capped(self, response: httpx.Response) -> bytes | None:
        """Streamed body, or None when max_bytes is exceeded (declared or observed)."""
        declared: str = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                return None
        return bytes(body)

    def _delay(
        self, *, status: int | None, exc: Exception | None, attempt: int, retry_after: str | None
    ) -> float | None:
        return retry_delay(
            status=status,
            exc=exc,
            attempt=attempt,
            max_attempts=self._max_attempts,
            retry_after=retry_after,
            now=self._now(),
            rng=self._rng,
        )
