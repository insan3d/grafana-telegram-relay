"""
Load validated environment settings and credentials from selected secret files.

Secret fields keep the existing *_FILE interface and mask values in model
representations. All settings are validated before application resources open.
"""

from pathlib import Path
from typing import Annotated

from humanfriendly import InvalidTimespan, parse_timespan
from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    FilePath,
    FiniteFloat,
    HttpUrl,
    PositiveInt,
    SecretStr,
    TypeAdapter,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRET_PATH = TypeAdapter(FilePath, config=ConfigDict(hide_input_in_errors=True))


def _read_secret(value: object) -> SecretStr:
    """Read a validated credential file without exposing its contents in errors."""

    return SecretStr(_SECRET_PATH.validate_python(value).read_text().strip())


def _parse_duration(value: object) -> object:
    """
    Parse a human-readable duration before numeric settings validation.

    Args:
        value: Environment string such as ``30d`` or ``1.5h``.

    Returns:
        Parsed seconds, or an existing numeric value for Pydantic to validate.

    Raises:
        ValueError: The value cannot be parsed as a duration.
    """

    if not isinstance(value, str):
        return value

    try:
        return parse_timespan(value)

    except InvalidTimespan, OverflowError:
        msg = "expected a human-readable duration"
        raise ValueError(msg) from None


type FileSecret = Annotated[SecretStr, BeforeValidator(_read_secret), Field(min_length=1)]
type Duration = Annotated[FiniteFloat, BeforeValidator(_parse_duration), Field(gt=0, strict=True)]


class Settings(BaseSettings):
    """Validate process configuration and file-backed credentials before startup."""

    model_config = SettingsConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
        validate_default=True,
    )

    bot_token: FileSecret = Field(validation_alias="BOT_TOKEN_FILE")
    chat_id: FileSecret = Field(validation_alias="CHAT_ID_FILE")
    grafana_token: FileSecret = Field(validation_alias="GRAFANA_TOKEN_FILE")
    grafana_url: HttpUrl = Field(validation_alias="GRAFANA_URL")
    state_db: Path = Field(default=Path("/data/state.db"), validation_alias="STATE_DB")
    templates_dir: Path = Field(default=Path("/etc/grafana-telegram-relay/templates"), validation_alias="TEMPLATES_DIR")
    message_template: str = Field(default="default.txt.j2", min_length=1, validation_alias="MESSAGE_TEMPLATE")
    canary_enabled: bool = Field(default=False, validation_alias="CANARY_ENABLED")
    canary_timeout_seconds: PositiveInt = Field(default=180, validation_alias="CANARY_TIMEOUT_SECONDS")
    retention_period: Duration = Field(default=30 * 86400, validation_alias="RETENTION_PERIOD")
