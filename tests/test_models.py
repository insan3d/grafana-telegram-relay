"""Protect strict payload validation, durable identity, and Grafana JSON aliases."""

import pytest
from pydantic import ValidationError

from grafana_telegram_relay.models import Alert
from tests.factories import alert_payload


@pytest.mark.parametrize("value", ["2026-10-17T12:00:00", "1970-01-01T00:00:00+01:00"])
def test_incident_times_require_a_timezone_and_the_unix_epoch(value: str) -> None:
    """Reject ambiguous or pre-epoch start times before deriving incident identity."""

    with pytest.raises(ValidationError):
        Alert.model_validate(alert_payload(starts_at=value))


def test_timezone_normalization_preserves_incident_identity() -> None:
    """Treat equivalent timezone representations as the same incident episode."""

    item = Alert.model_validate(alert_payload(starts_at="2026-10-17T15:00:00+03:00"))

    assert item.starts_at == "2026-10-17T12:00:00+00:00"
    assert item.key == "grafana-test:2026-10-17T12:00:00+00:00"
    assert item.key == Alert.model_validate(alert_payload()).key


def test_minimal_alert_defaults_null_results_and_ignores_firing_end() -> None:
    """Accept Grafana's optional values without requiring recovery data while firing."""

    item = Alert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "Test"},
            "startsAt": "2026-10-17T12:00:00Z",
            "endsAt": None,
            "values": None,
            "extraGrafanaField": "ignored",
        }
    )

    assert item.annotations == {}
    assert item.values == {}
    assert item.panel_url == ""
    assert item.ends_at == ""
    assert item.fingerprint


def test_missing_fingerprint_is_derived_from_labels_independently_of_order() -> None:
    """Keep incident identity stable when Grafana changes JSON object ordering."""

    first = alert_payload()
    first["fingerprint"] = ""
    second = alert_payload()
    second["fingerprint"] = ""
    second["labels"] = {"instance": "grafana", "alertname": "GrafanaTest"}

    assert Alert.model_validate(first).key == Alert.model_validate(second).key
