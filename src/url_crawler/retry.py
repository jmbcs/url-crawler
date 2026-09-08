from __future__ import annotations

import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0

_RETRY_AFTER_STATUSES: frozenset[int] = frozenset({429, 503})


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def is_retryable_exception(exc: BaseException) -> bool:
    # Not TransportError: its LocalProtocolError and UnsupportedProtocol leaves are caller bugs.
    return isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError)


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait per a Retry-After header, clamped to [0, MAX_RETRY_AFTER_SECONDS]."""
    if value is None:
        return None
    seconds = _delta_seconds(value)
    if seconds is None:
        seconds = _seconds_until_http_date(value, now)
    if seconds is None:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _delta_seconds(value: str) -> float | None:
    try:
        # Clamp in the int domain: a header of 309+ digits overflows float().
        return float(min(max(int(value), 0), MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        return None


def _seconds_until_http_date(value: str, now: datetime) -> float | None:
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (when - now).total_seconds()


def backoff_delay(attempt: int, rng: random.Random) -> float:
    return rng.uniform(0.0, min(MAX_BACKOFF_SECONDS, 0.5 * 2**attempt))


def retry_delay(
    *,
    status: int | None,
    exc: BaseException | None,
    attempt: int,
    max_attempts: int,
    retry_after: str | None,
    now: datetime,
    rng: random.Random,
) -> float | None:
    """Seconds to wait before attempt+1, or None to give up."""
    if attempt >= max_attempts:
        return None
    if exc is not None and not is_retryable_exception(exc):
        return None
    if status is not None and not is_retryable_status(status):
        return None
    if status in _RETRY_AFTER_STATUSES:
        delay = parse_retry_after(retry_after, now)
        if delay is not None:
            return delay
    return backoff_delay(attempt, rng)
