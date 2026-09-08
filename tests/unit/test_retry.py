from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from url_crawler.retry import (
    MAX_BACKOFF_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    RETRYABLE_STATUSES,
    backoff_delay,
    is_retryable_exception,
    is_retryable_status,
    parse_retry_after,
    retry_delay,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_retryable_statuses(status: int) -> None:
    assert is_retryable_status(status) is True


@pytest.mark.parametrize("status", [200, 204, 301, 400, 401, 403, 404, 410, 418, 451, 501, 505])
def test_terminal_statuses(status: int) -> None:
    assert is_retryable_status(status) is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("slow"),
        httpx.ConnectTimeout("slow connect"),
        httpx.ReadTimeout("slow read"),
        httpx.WriteTimeout("slow write"),
        httpx.PoolTimeout("no connection"),
        httpx.NetworkError("network"),
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.WriteError("broken pipe"),
        httpx.CloseError("close"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_retryable_exceptions(exc: Exception) -> None:
    assert is_retryable_exception(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        httpx.LocalProtocolError("bad request line"),
        httpx.UnsupportedProtocol("unsupported scheme"),
        httpx.InvalidURL("no host"),
        httpx.ProtocolError("protocol"),
        httpx.TooManyRedirects("loop"),
        ValueError("nonsense"),
        KeyboardInterrupt(),
    ],
)
def test_terminal_exceptions(exc: BaseException) -> None:
    assert is_retryable_exception(exc) is False


def test_parse_retry_after_delta_seconds() -> None:
    assert parse_retry_after("12", NOW) == 12.0


def test_parse_retry_after_strips_whitespace() -> None:
    assert parse_retry_after("  7 ", NOW) == 7.0


def test_parse_retry_after_http_date() -> None:
    value = format_datetime(NOW + timedelta(seconds=20), usegmt=True)
    assert parse_retry_after(value, NOW) == 20.0


def test_parse_retry_after_past_http_date_is_zero() -> None:
    value = format_datetime(NOW - timedelta(seconds=60), usegmt=True)
    assert parse_retry_after(value, NOW) == 0.0


def test_parse_retry_after_negative_delta_is_zero() -> None:
    assert parse_retry_after("-5", NOW) == 0.0


@pytest.mark.parametrize("value", [None, "", "soon", "12 seconds", "1.5", "NaN"])
def test_parse_retry_after_garbage(value: str | None) -> None:
    assert parse_retry_after(value, NOW) is None


def test_parse_retry_after_caps_delta() -> None:
    assert parse_retry_after("600", NOW) == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_caps_http_date() -> None:
    value = format_datetime(NOW + timedelta(hours=1), usegmt=True)
    assert parse_retry_after(value, NOW) == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_huge_delta_is_capped() -> None:
    assert parse_retry_after("9" * 400, NOW) == MAX_RETRY_AFTER_SECONDS


def test_parse_retry_after_huge_negative_delta_is_zero() -> None:
    assert parse_retry_after("-" + "9" * 400, NOW) == 0.0


def test_parse_retry_after_naive_http_date_is_utc() -> None:
    assert parse_retry_after("Tue, 08 Sep 2026 12:00:20 -0000", NOW) == 20.0


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5, 10])
def test_backoff_delay_stays_within_full_jitter_bounds(attempt: int) -> None:
    rng = random.Random(0)
    ceiling = min(MAX_BACKOFF_SECONDS, 0.5 * 2**attempt)
    delays = [backoff_delay(attempt, rng) for _ in range(50)]
    assert all(0.0 <= delay <= ceiling for delay in delays)


def test_backoff_delay_is_deterministic_for_a_seeded_rng() -> None:
    first = [backoff_delay(attempt, random.Random(0)) for attempt in (1, 2, 3)]
    second = [backoff_delay(attempt, random.Random(0)) for attempt in (1, 2, 3)]
    assert first == second


def test_backoff_delay_ceiling_is_capped() -> None:
    assert backoff_delay(20, random.Random(0)) <= MAX_BACKOFF_SECONDS


class RecordingRandom(random.Random):
    """Records the bounds handed to uniform() so the jitter ceiling can be asserted."""

    bounds: list[tuple[float, float]]

    def __init__(self, seed: int = 0) -> None:
        super().__init__(seed)
        self.bounds = []

    def uniform(self, a: float, b: float) -> float:
        self.bounds.append((a, b))
        return super().uniform(a, b)


def test_backoff_delay_ceiling_doubles_per_attempt_until_the_cap() -> None:
    rng = RecordingRandom()
    for attempt in (1, 2, 3, 4, 5):
        backoff_delay(attempt, rng)
    assert rng.bounds == [(0.0, 1.0), (0.0, 2.0), (0.0, 4.0), (0.0, 8.0), (0.0, 8.0)]


def decide(
    *,
    status: int | None = None,
    exc: BaseException | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
    retry_after: str | None = None,
    seed: int = 0,
) -> float | None:
    return retry_delay(
        status=status,
        exc=exc,
        attempt=attempt,
        max_attempts=max_attempts,
        retry_after=retry_after,
        now=NOW,
        rng=random.Random(seed),
    )


def test_retry_delay_gives_up_on_the_last_attempt() -> None:
    assert decide(status=503, attempt=3, max_attempts=3) is None


def test_retry_delay_gives_up_beyond_the_last_attempt() -> None:
    assert decide(status=503, attempt=4, max_attempts=3) is None


def test_retry_delay_gives_up_on_a_terminal_status() -> None:
    assert decide(status=404) is None


def test_retry_delay_gives_up_on_a_terminal_exception() -> None:
    assert decide(exc=httpx.LocalProtocolError("bad")) is None


def test_retry_delay_backs_off_on_a_retryable_status() -> None:
    delay = decide(status=500)
    assert delay is not None
    assert 0.0 <= delay <= 1.0


def test_retry_delay_backs_off_on_a_retryable_exception() -> None:
    delay = decide(exc=httpx.ConnectTimeout("slow"))
    assert delay is not None
    assert 0.0 <= delay <= 1.0


@pytest.mark.parametrize("status", [429, 503])
def test_retry_delay_prefers_retry_after(status: int) -> None:
    assert decide(status=status, retry_after="9") == 9.0


@pytest.mark.parametrize("status", [408, 425, 500, 502, 504])
def test_retry_delay_ignores_retry_after_on_other_statuses(status: int) -> None:
    delay = decide(status=status, retry_after="9")
    assert delay is not None
    assert 0.0 <= delay <= 1.0


def test_retry_delay_falls_back_to_backoff_when_retry_after_is_garbage() -> None:
    delay = decide(status=429, retry_after="soon")
    assert delay is not None
    assert 0.0 <= delay <= 1.0


def test_retry_delay_backs_off_when_retry_after_is_missing() -> None:
    delay = decide(status=429, retry_after=None)
    assert delay is not None
    assert 0.0 <= delay <= 1.0


def test_retry_delay_caps_a_long_retry_after() -> None:
    assert decide(status=429, retry_after="600") == MAX_RETRY_AFTER_SECONDS


def test_retry_delay_caps_a_huge_retry_after() -> None:
    assert decide(status=429, retry_after="9" * 400) == MAX_RETRY_AFTER_SECONDS
