"""
Validate Grafana payloads and define typed snapshots of persistent state.

Pydantic models validate untrusted input before persistence or delivery. JSON
aliases preserve Grafana's field names in SQLite and the template context.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, TypedDict

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

type AlertStatus = Literal["firing", "resolved"]
type ParseMode = Literal["HTML"] | None
type AckResult = Literal["acknowledged", "already_acknowledged", "resolved", "not_found"]
type JsonValue = bool | int | FiniteFloat | str | list[JsonValue] | dict[str, JsonValue] | None

TEXT = TypeAdapter(str, config=ConfigDict(strict=True))
_TIMESTAMP = TypeAdapter(
    Annotated[AwareDatetime, Field(ge=datetime(1970, 1, 1, tzinfo=UTC))], config=ConfigDict(strict=True)
)


def timestamp(value: str) -> str:
    """
    Normalize a timezone-aware timestamp at or after the Unix epoch to UTC.

    Args:
        value: Grafana timestamp validated as a string.

    Returns:
        The ISO 8601 representation in UTC.

    Raises:
        ValueError: The timestamp is invalid, naive, or predates the Unix epoch.
    """

    return _TIMESTAMP.validate_python(datetime.fromisoformat(value)).astimezone(UTC).isoformat()


class Alert(BaseModel):
    """Represent a strictly validated incident update with Grafana JSON aliases."""

    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False, validate_default=True)

    status: AlertStatus
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: Annotated[str, AfterValidator(timestamp)] = Field(alias="startsAt")
    ends_at: str = Field(default="", alias="endsAt")
    values: dict[str, JsonValue] = Field(default_factory=dict)
    panel_url: str = Field(default="", alias="panelURL")
    dashboard_url: str = Field(default="", alias="dashboardURL")
    generator_url: str = Field(default="", alias="generatorURL")
    silence_url: str = Field(default="", alias="silenceURL")
    fingerprint: str = ""

    @field_validator("labels")
    @classmethod
    def require_alertname(cls, value: dict[str, str]) -> dict[str, str]:
        """Require the incident name used by delivery and notification templates."""

        if not value.get("alertname"):
            msg = "alertname is required"
            raise ValueError(msg)

        return value

    @field_validator("ends_at", mode="before")
    @classmethod
    def normalize_end(cls, value: object, info: ValidationInfo[None]) -> str:
        """Require a recovery timestamp only for resolved incidents."""

        return timestamp(TEXT.validate_python(value)) if info.data.get("status") == "resolved" else ""

    @field_validator("values", mode="before")
    @classmethod
    def default_values(cls, value: object) -> object:
        """Treat Grafana's null query results as an empty result map."""

        return {} if value is None else value

    @model_validator(mode="after")
    def derive_fingerprint(self) -> Self:
        """Derive a stable label fingerprint when Grafana omits it."""

        if not self.fingerprint:
            self.fingerprint = hashlib.sha256(json.dumps(self.labels, sort_keys=True).encode()).hexdigest()

        return self

    @property
    def key(self) -> str:
        """Identify one incident episode independently of its delivery version."""

        return self.fingerprint + ":" + self.starts_at


ALERTS = TypeAdapter(Annotated[list[Alert], Field(min_length=1)], config=ConfigDict(strict=True))


class Webhook(BaseModel):
    """Validate a complete Grafana notification before any state is changed."""

    model_config = ConfigDict(strict=True, extra="ignore")

    alerts: list[Alert] = Field(min_length=1)
    truncated_alerts: int = Field(default=0, alias="truncatedAlerts", ge=0, le=0)

    def require_canary(self) -> None:
        """
        Require every alert to be a firing Grafana canary heartbeat.

        Raises:
            ValueError: At least one entry is not an expected canary heartbeat.
        """

        if any(
            item.status != "firing"
            or item.labels.get("alertname") != "GrafanaCanary"
            or item.labels.get("canary") != "true"
            for item in self.alerts
        ):
            msg = "expected a firing GrafanaCanary"
            raise ValueError(msg)


def acknowledgement_token(key: str) -> str:
    """Derive a compact incident identifier that fits Telegram callback data."""

    return hashlib.sha256(key.encode()).hexdigest()[:32]


class Acknowledgement(TypedDict):
    """Record the first operator and the incident version created by their ACK."""

    user_id: int
    name: str
    at: float
    version: int


class Incident(TypedDict):
    """Describe an immutable snapshot selected from the delivery queue."""

    key: str
    payload: str
    version: int
    delivered: int
    message_id: int | None
    resolution_message_id: int | None
    photo: int
    attempts: int
    next_attempt: float
    updated: float
    acknowledgement: Acknowledgement | None


class StoreMetrics(TypedDict):
    """Expose durable gauges without leaking database driver value types."""

    pending: int
    active: int
    oldest_pending: float
    last_seen: float


class ReminderTask(TypedDict):
    """Carry one due copy or deletion with its durable retry state."""

    action: Literal["send", "delete"]
    key: str
    payload: str
    message_id: int
    attempts: int


def canary_alert(start: float, status: AlertStatus = "firing", end: float | None = None) -> Alert:
    """
    Construct an internal watchdog update using the normal alert schema.

    Args:
        start: Unix timestamp when missing heartbeat was detected.
        status: Current watchdog incident state.
        end: Recovery timestamp, if the incident has resolved.

    Returns:
        A validated alert suitable for normal persistent delivery.
    """

    return Alert(
        fingerprint="internal-grafana-canary",
        status=status,
        startsAt=datetime.fromtimestamp(start, UTC).isoformat(),
        endsAt=datetime.fromtimestamp(end, UTC).isoformat() if end is not None else "",
        labels={"alertname": "GrafanaCanaryMissing", "severity": "critical"},
        annotations={
            "summary": "Grafana canary heartbeat is missing",
            "description": "Check Grafana, alert evaluation and webhook delivery to the Telegram bot.",
        },
    )
