"""Build independent Grafana payloads, validated alerts, and isolated process settings."""

lazy from pathlib import Path

from grafana_telegram_relay.models import Alert
lazy from grafana_telegram_relay.models import AlertStatus


def alert_payload(
    status: AlertStatus = "firing", *, starts_at: str = "2026-10-17T12:00:00Z", **fields: object
) -> dict[str, object]:
    """Build raw Grafana JSON with optional field overrides, including invalid values."""

    return {
        "status": status,
        "fingerprint": "grafana-test",
        "startsAt": starts_at,
        "endsAt": "2026-10-17T12:10:00Z" if status == "resolved" else "",
        "labels": {"alertname": "GrafanaTest", "instance": "grafana"},
        "annotations": {"summary": "Test incident"},
        "values": {"A": 1},
        "panelURL": "",
        "dashboardURL": "",
        "generatorURL": "",
        "silenceURL": "",
        **fields,
    }


def alert(status: AlertStatus = "firing", *, starts_at: str = "2026-10-17T12:00:00Z") -> Alert:
    """Build a validated alert for delivery, rendering, buttons, and templates."""

    return Alert.model_validate(alert_payload(status, starts_at=starts_at))


def environment(directory: Path) -> dict[str, str]:
    """Create offline credential files and return the required process environment."""

    directory.mkdir(parents=True, exist_ok=True)
    values = {
        "GRAFANA_URL": "http://grafana:3000/grafana",
        "STATE_DB": str(directory / "state.db"),
    }

    for name, content in (
        ("BOT_TOKEN_FILE", "123456:offline-test-token"),
        ("CHAT_ID_FILE", "-123"),
        ("GRAFANA_TOKEN_FILE", "offline-grafana-token"),
    ):
        path = directory / name.lower()
        path.write_text(content + "\n")
        values[name] = str(path)

    return values
