from __future__ import annotations

import asyncio
import random
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import httpx

from url_crawler.models import FetchError, FetchErrorKind, FetchResult
from url_crawler.retry import retry_delay

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_GZIP_WBITS = 16 + zlib.MAX_WBITS

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


class _Inflater:
    """Incremental inflater bounded per call, with httpx's raw-deflate retry on the first chunk."""

    def __init__(self, wbits: int, *, deflate_fallback: bool = False) -> None:
        self._decompressor = zlib.decompressobj(wbits)
        self._fallback_wbits = -zlib.MAX_WBITS if deflate_fallback else None

    def inflate(self, chunk: bytes, max_length: int) -> bytes:
        fallback = self._fallback_wbits
        self._fallback_wbits = None
        if fallback is None:
            return self._decompress(chunk, max_length)
        try:
            return self._decompressor.decompress(chunk, max_length)
        except zlib.error:
            self._decompressor = zlib.decompressobj(fallback)
        return self._decompress(chunk, max_length)

    def _decompress(self, chunk: bytes, max_length: int) -> bytes:
        try:
            return self._decompressor.decompress(chunk, max_length)
        except zlib.error as exc:
            raise httpx.DecodingError(str(exc)) from exc


def _inflater_for(content_encoding: str) -> _Inflater | None:
    encoding = content_encoding.strip().lower()
    if encoding in ("gzip", "x-gzip"):
        return _Inflater(_GZIP_WBITS)
    if encoding == "deflate":
        return _Inflater(zlib.MAX_WBITS, deflate_fallback=True)
    if encoding in ("", "identity"):
        return None
    raise httpx.DecodingError(f"unsupported content-encoding {encoding!r}")


def _declares_more_than(response: httpx.Response, max_bytes: int) -> bool:
    """Whether the response announces a body over max_bytes, before any of it is read."""
    declared: str = response.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > max_bytes


async def read_bounded(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Up to max_bytes of body, and whether the raw or the decoded stream ran past that cap."""
    inflater = _inflater_for(response.headers.get("content-encoding", ""))
    # The transport stream rather than aiter_raw(): identical bytes, and it also serves a
    # response whose body httpx has already buffered.
    stream = cast(httpx.AsyncByteStream, response.stream)
    body = bytearray()
    raw_bytes = 0
    async for chunk in stream:
        capped = chunk[: max_bytes - raw_bytes]
        raw_bytes += len(chunk)
        if inflater is None:
            body.extend(capped)
        else:
            body.extend(inflater.inflate(capped, max_bytes + 1 - len(body)))
        if raw_bytes > max_bytes or len(body) > max_bytes:
            return bytes(body[:max_bytes]), True
    return bytes(body), False


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
        if _declares_more_than(response, self._max_bytes):
            return self._too_large(url, status, attempt)
        body, too_large = await read_bounded(response, self._max_bytes)
        if too_large:
            return self._too_large(url, status, attempt)
        return FetchResult(url, status, body, mime_type, None, attempt)

    def _too_large(self, url: str, status: int, attempt: int) -> FetchError:
        message = f"body larger than {self._max_bytes} bytes"
        return FetchError(url, FetchErrorKind.TOO_LARGE, status, message, attempt)

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
