"""
Build shared Telegram incident controls from validated Grafana metadata.

Acknowledgement occupies its own row. Navigation buttons appear only when an
absolute HTTP URL is available; a panel URL opens the corresponding dashboard.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from yarl import URL

from .models import acknowledgement_token
lazy from .models import Alert, Incident


def _http_url(value: str) -> URL | None:
    """Accept absolute HTTP links that Telegram can display as URL buttons."""

    try:
        parsed = URL(value)
        host, port = parsed.host, parsed.port

    except ValueError:
        return None

    if (
        parsed.scheme not in ("http", "https")
        or not host
        or port == 0
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        return None

    if host == "localhost":
        # Telegram accepts numeric loopback addresses but rejects localhost buttons.
        return parsed.with_host("127.0.0.1")

    return parsed


def without_url_buttons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Retain callback controls when Telegram rejects optional navigation URLs."""

    if markup is None:
        return None

    rows = [[button for button in row if button.url is None] for row in markup.inline_keyboard]
    remaining = [row for row in rows if row]

    return InlineKeyboardMarkup(remaining) if remaining else None


def _dashboard_url(item: Alert) -> URL | None:
    """Prefer the dashboard URL and remove panel expansion from a panel fallback."""

    dashboard = _http_url(item.dashboard_url)

    if dashboard:
        return dashboard

    panel = _http_url(item.panel_url)

    if panel is None:
        return None

    return panel.without_query_params("viewPanel")


def incident_keyboard(item: Alert, row: Incident) -> InlineKeyboardMarkup | None:
    """
    Create acknowledgement and navigation rows for an incident snapshot.

    Args:
        item: Validated Grafana alert with annotations and generated URLs.
        row: Persisted incident version and optional acknowledgement.

    Returns:
        Shared inline controls, or None when no actions remain.
    """

    rows: list[list[InlineKeyboardButton]] = []

    if item.status == "firing" and row["acknowledgement"] is None:
        data = "ack:" + acknowledgement_token(row["key"])
        rows.append([InlineKeyboardButton("Acknowledge", callback_data=data)])

    links = (
        ("Runbook", _http_url(item.annotations.get("runbook_url", ""))),
        ("Grafana", _dashboard_url(item)),
        ("Silence", _http_url(item.silence_url)),
    )
    navigation = [InlineKeyboardButton(label, url=str(url)) for label, url in links if url is not None]

    if navigation:
        rows.append(navigation)

    return InlineKeyboardMarkup(rows) if rows else None
