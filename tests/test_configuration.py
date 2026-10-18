"""Verify environment validation before application resources are opened."""

import asyncio
lazy from pathlib import Path

import pytest

from grafana_telegram_relay.app import application
from grafana_telegram_relay.settings import Settings
from tests.factories import environment


@pytest.fixture(autouse=True)
def settings_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Provide isolated credential files and deterministic environment defaults."""

    for name, value in environment(tmp_path).items():
        monkeypatch.setenv(name, value)

    for name in ("RETENTION_PERIOD", "CANARY_TIMEOUT_SECONDS", "TEMPLATES_DIR", "MESSAGE_TEMPLATE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("value", ["0s", "-1h", "9" * 400 + "d"])
def test_retention_requires_a_finite_positive_window(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Reject retention windows that cannot define a safe cleanup deadline."""

    monkeypatch.setenv("RETENTION_PERIOD", value)

    with pytest.raises(ValueError, match="RETENTION_PERIOD"):
        Settings()


def test_settings_load_defaults_and_mask_file_credentials() -> None:
    """Preserve mounted secret files and keep their contents out of representations."""

    settings = Settings()

    assert settings.retention_period == 2592000
    assert settings.canary_timeout_seconds == 180
    assert settings.message_template == "default.txt.j2"
    assert str(settings.grafana_url) == "http://grafana:3000/grafana"
    assert settings.bot_token.get_secret_value() == "123456:offline-test-token"
    assert settings.chat_id.get_secret_value() == "-123"
    assert settings.grafana_token.get_secret_value() == "offline-grafana-token"
    assert "offline" not in repr(settings)
    assert "-123" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GRAFANA_URL", "ftp://grafana/path"),
        ("CANARY_TIMEOUT_SECONDS", "0"),
        ("MESSAGE_TEMPLATE", ""),
    ],
)
def test_settings_reject_invalid_process_configuration(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Fail startup on malformed URLs, empty template names, or invalid timeouts."""

    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings()


def test_settings_require_existing_nonempty_secret_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject missing or empty credentials instead of starting delivery workers."""

    path = tmp_path / "missing-token"
    monkeypatch.setenv("BOT_TOKEN_FILE", str(path))

    with pytest.raises(ValueError, match="BOT_TOKEN_FILE"):
        Settings()

    path.write_text(" \n")

    with pytest.raises(ValueError, match="BOT_TOKEN_FILE"):
        Settings()


def test_invalid_settings_do_not_create_the_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject invalid retention before opening SQLite or HTTP transports."""

    monkeypatch.setenv("RETENTION_PERIOD", "invalid")

    with pytest.raises(ValueError, match="RETENTION_PERIOD"):
        asyncio.run(application())

    assert not (tmp_path / "state.db").exists()
