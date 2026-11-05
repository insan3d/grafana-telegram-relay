"""Build independent Grafana payloads, validated alerts, and isolated process settings."""

import json
import sqlite3
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


def legacy_database(path: Path) -> None:
    """Create the pre-grouping schema with real IDs, ACK, delivery retries and orphan cleanup."""

    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE incidents (
                key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, delivered INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER, photo INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL
            );
            CREATE TABLE acknowledgements (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(key) ON DELETE CASCADE,
                ack_user_id INTEGER NOT NULL, ack_name TEXT NOT NULL,
                ack_at REAL NOT NULL, ack_version INTEGER NOT NULL
            );
            CREATE TABLE resolution_replies (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(key) ON DELETE CASCADE,
                message_id INTEGER NOT NULL
            );
            CREATE TABLE reminder_messages (
                message_id INTEGER PRIMARY KEY, incident_key TEXT NOT NULL,
                sent_at REAL NOT NULL, next_at REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE reminder_schedule (
                incident_key TEXT PRIMARY KEY REFERENCES incidents(key) ON DELETE CASCADE,
                next_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO reminder_schedule VALUES('scheduled',2500,3);
            INSERT INTO acknowledgements VALUES('legacy',7,'Alice',999,4);
            INSERT INTO resolution_replies VALUES('legacy',92);
            INSERT INTO reminder_messages VALUES(93,'expired',10,1500,2);
        """)
        db.execute("INSERT INTO incidents VALUES(?,?,5,4,91,1,2,2000,1000)", ("legacy", json.dumps(alert_payload())))
        db.execute(
            "INSERT INTO incidents VALUES(?,?,1,1,94,0,0,0,1000)",
            ("scheduled", json.dumps(alert_payload(fingerprint="scheduled"))),
        )
