from __future__ import annotations

import pytest

from url_crawler_service.settings import Settings, SettingsError

DATABASE_URL = "postgresql+asyncpg://crawler:crawler@localhost:5432/crawler"


def test_defaults() -> None:
    settings = Settings.from_env({"DATABASE_URL": DATABASE_URL})

    assert settings.database_url == DATABASE_URL
    assert settings.worker_poll_seconds == 1.0
    assert settings.heartbeat_seconds == 5.0
    assert settings.lease_seconds == 30.0
    assert settings.max_attempts == 3
    assert settings.page_batch_size == 100
    assert settings.page_flush_seconds == 0.2
    assert settings.api_host == "0.0.0.0"
    assert settings.api_port == 8000


def test_every_field_reads_its_environment_variable() -> None:
    settings = Settings.from_env(
        {
            "DATABASE_URL": DATABASE_URL,
            "WORKER_POLL_SECONDS": "0.5",
            "HEARTBEAT_SECONDS": "2",
            "LEASE_SECONDS": "12.5",
            "MAX_ATTEMPTS": "7",
            "PAGE_BATCH_SIZE": "250",
            "PAGE_FLUSH_SECONDS": "0.05",
            "API_HOST": "127.0.0.1",
            "API_PORT": "9000",
        }
    )

    assert settings.worker_poll_seconds == 0.5
    assert settings.heartbeat_seconds == 2.0
    assert settings.lease_seconds == 12.5
    assert settings.max_attempts == 7
    assert settings.page_batch_size == 250
    assert settings.page_flush_seconds == 0.05
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 9000


def test_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("API_PORT", "8123")

    settings = Settings.from_env()

    assert settings.database_url == DATABASE_URL
    assert settings.api_port == 8123


@pytest.mark.parametrize("env", [{}, {"DATABASE_URL": ""}])
def test_missing_database_url_raises(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        Settings.from_env(env)


@pytest.mark.parametrize(
    "name",
    [
        "WORKER_POLL_SECONDS",
        "HEARTBEAT_SECONDS",
        "LEASE_SECONDS",
        "MAX_ATTEMPTS",
        "PAGE_BATCH_SIZE",
        "PAGE_FLUSH_SECONDS",
        "API_PORT",
    ],
)
def test_unparsable_number_raises_naming_the_variable(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        Settings.from_env({"DATABASE_URL": DATABASE_URL, name: "not-a-number"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WORKER_POLL_SECONDS", "0"),
        ("WORKER_POLL_SECONDS", "-1"),
        ("HEARTBEAT_SECONDS", "0"),
        ("LEASE_SECONDS", "nan"),
        ("LEASE_SECONDS", "inf"),
        ("PAGE_FLUSH_SECONDS", "-0.5"),
        ("MAX_ATTEMPTS", "0"),
        ("PAGE_BATCH_SIZE", "0"),
        ("API_PORT", "0"),
        ("API_PORT", "70000"),
    ],
)
def test_out_of_range_value_raises_naming_the_variable(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        Settings.from_env({"DATABASE_URL": DATABASE_URL, name: value})


def test_lease_shorter_than_heartbeat_raises_naming_both() -> None:
    with pytest.raises(SettingsError, match=r"LEASE_SECONDS.*HEARTBEAT_SECONDS"):
        Settings.from_env(
            {"DATABASE_URL": DATABASE_URL, "HEARTBEAT_SECONDS": "60", "LEASE_SECONDS": "5"}
        )


def test_lease_equal_to_heartbeat_is_accepted() -> None:
    settings = Settings.from_env(
        {"DATABASE_URL": DATABASE_URL, "HEARTBEAT_SECONDS": "10", "LEASE_SECONDS": "10"}
    )

    assert settings.lease_seconds == settings.heartbeat_seconds == 10.0
