from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite


class SettingsError(Exception):
    """Raised when the environment is missing a setting or holds an unusable value."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    worker_poll_seconds: float = 1.0
    heartbeat_seconds: float = 5.0
    lease_seconds: float = 30.0
    max_attempts: int = 3
    page_batch_size: int = 100
    page_flush_seconds: float = 0.2
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    def __post_init__(self) -> None:
        _positive("WORKER_POLL_SECONDS", self.worker_poll_seconds)
        _positive("HEARTBEAT_SECONDS", self.heartbeat_seconds)
        _positive("LEASE_SECONDS", self.lease_seconds)
        _positive("PAGE_FLUSH_SECONDS", self.page_flush_seconds)
        _at_least_one("MAX_ATTEMPTS", self.max_attempts)
        _at_least_one("PAGE_BATCH_SIZE", self.page_batch_size)
        if not 0 < self.api_port < 65536:
            raise SettingsError(f"API_PORT must be between 1 and 65535, got {self.api_port}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from environment variables named after the fields, upper-cased."""
        source = os.environ if env is None else env
        database_url = source.get("DATABASE_URL")
        if not database_url:
            raise SettingsError("DATABASE_URL is required")
        defaults = cls(database_url=database_url)
        return replace(
            defaults,
            worker_poll_seconds=_float(source, "WORKER_POLL_SECONDS", defaults.worker_poll_seconds),
            heartbeat_seconds=_float(source, "HEARTBEAT_SECONDS", defaults.heartbeat_seconds),
            lease_seconds=_float(source, "LEASE_SECONDS", defaults.lease_seconds),
            max_attempts=_int(source, "MAX_ATTEMPTS", defaults.max_attempts),
            page_batch_size=_int(source, "PAGE_BATCH_SIZE", defaults.page_batch_size),
            page_flush_seconds=_float(source, "PAGE_FLUSH_SECONDS", defaults.page_flush_seconds),
            api_host=source.get("API_HOST", defaults.api_host),
            api_port=_int(source, "API_PORT", defaults.api_port),
        )


def _positive(name: str, value: float) -> None:
    if not isfinite(value) or value <= 0:
        raise SettingsError(f"{name} must be a positive number, got {value}")


def _at_least_one(name: str, value: int) -> None:
    if value < 1:
        raise SettingsError(f"{name} must be >= 1, got {value}")


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from exc


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from exc
