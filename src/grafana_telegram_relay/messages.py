"""
Format incident messages with sandboxed Jinja templates and safe fallbacks.

Text and HTML use separate environments so includes share the selected escaping
mode. Length limits apply to decoded text without splitting UTF-16 characters.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import override
lazy from collections.abc import Sequence

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .metrics import TEMPLATE_FAILURES
lazy from .models import Acknowledgement, Alert, ParseMode

log = logging.getLogger(__name__)
MAX_ACK_NAME_UNITS = 128
BUILTIN_DIRECTORY = Path(__file__).resolve().parent / "templates"


def clip(text: str, limit: int) -> str:
    """
    Clip text to a UTF-16 budget without splitting surrogate pairs.

    Args:
        text: Unformatted Unicode text.
        limit: Maximum number of UTF-16 code units.

    Returns:
        The longest fitting prefix.
    """

    return text.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")


def fallback_text(item: Alert) -> str:
    """
    Build an emergency message independently of template files.

    Args:
        item: Validated incident update.

    Returns:
        Plain incident details suitable for delivery after a template failure.
    """

    lines = [f"{'🔴 FIRING' if item.status == 'firing' else '🟢 RESOLVED'} · " + item.labels["alertname"]]

    if mention := item.labels.get("mention"):
        lines.append(mention)

    lines.extend("\n" + item.annotations[key] for key in ("summary", "description") if item.annotations.get(key))

    labels = "\n".join(
        f"{key}: {value}"
        for key, value in sorted(item.labels.items())
        if key not in ("alertname", "grafana_folder", "mention")
    )

    if labels:
        lines.append("\n" + labels)

    lines.append("\nStarted: " + item.starts_at)

    if item.status == "resolved":
        lines.append("Resolved: " + item.ends_at)

    if item.values:
        lines.append("\nValues: " + json.dumps(item.values, ensure_ascii=False))

    return "\n".join(lines)


class PlainText(HTMLParser):
    """Collect decoded text for HTML clipping and Telegram parse-error fallback."""

    def __init__(self) -> None:
        """Initialize the parser and its decoded text fragments."""

        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    @override
    def handle_data(self, data: str) -> None:
        """
        Append a decoded text fragment.

        Args:
            data: Text emitted by the HTML parser.
        """

        self.parts.append(data)

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """
        Preserve explicit line breaks while discarding markup.

        Args:
            tag: HTML element name.
            attrs: Parsed attributes, unused for plain text conversion.
        """

        if tag == "br":
            self.parts.append("\n")


@dataclass(frozen=True)
class Message:
    """Carry formatted Telegram content and its plain-text fallback."""

    text: str
    parse_mode: ParseMode
    plain: str


class MessageTemplates:
    """Load message templates on demand and preserve delivery on author errors."""

    def __init__(self, directory: str | Path | None = None, default: str = "default.html.j2") -> None:
        """
        Initialize text and HTML environments with an optional override path.

        Args:
            directory: Directory searched before the packaged templates.
            default: Template selected when the alert has no override annotation.
        """

        paths = [str(BUILTIN_DIRECTORY)]

        if directory is not None:
            paths.insert(0, str(directory))

        self.text_environment = self.environment_for(paths, html=False)
        self.html_environment = self.environment_for(paths, html=True)
        self.default = default

    @staticmethod
    def environment_for(paths: Sequence[str], *, html: bool = False) -> ImmutableSandboxedEnvironment:
        """
        Create an uncached environment with a consistent escaping mode.

        Args:
            paths: Template search directories in priority order.
            html: Whether all templates and includes escape dynamic content.

        Returns:
            A sandboxed environment that rereads files on every notification.
        """

        return ImmutableSandboxedEnvironment(
            loader=FileSystemLoader(paths),
            undefined=StrictUndefined,
            autoescape=html,
            auto_reload=True,
            cache_size=0,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def __call__(self, item: Alert, limit: int = 4096, *, acknowledgement: Acknowledgement | None = None) -> Message:
        """
        Render a selected template, falling back to emergency plain text.

        Args:
            item: Validated alert containing message data and template selection.
            limit: Telegram text or photo-caption budget in UTF-16 code units.
            acknowledgement: First operator to acknowledge this incident, if any.

        Returns:
            A bounded message with its Telegram parse mode and plain fallback.
        """

        footer = (
            "\n\nAcknowledged by " + clip(acknowledgement["name"], MAX_ACK_NAME_UNITS)
            if acknowledgement is not None
            else ""
        )
        budget = max(0, limit - len(footer.encode("utf-16-le")) // 2)
        now = datetime.now(UTC)
        start = datetime.fromisoformat(item.starts_at)
        end = datetime.fromisoformat(item.ends_at) if item.ends_at else now

        context: dict[str, object] = {
            "alert": item.model_dump(by_alias=True),
            "acknowledgement": acknowledgement,
            "status": item.status,
            "labels": item.labels,
            "annotations": item.annotations,
            "values": item.values,
            "starts_at": item.starts_at,
            "ends_at": item.ends_at,
            "duration_seconds": max(0, (end - start).total_seconds()),
            "now": now,
            "dashboard_url": item.dashboard_url,
            "panel_url": item.panel_url,
            "generator_url": item.generator_url,
            "silence_url": item.silence_url,
            "fingerprint": item.fingerprint,
        }

        name = item.annotations.get("telegram_template", self.default)

        try:
            message = self._render(name, context, limit=budget)

        # User-authored templates can fail in arbitrary ways; retain plain delivery.
        except Exception as error:  # noqa: BLE001
            TEMPLATE_FAILURES.inc()
            log.warning("Message template failed: %s; using default", type(error).__name__)
            plain = clip(fallback_text(item), budget)
            message = Message(plain, None, plain)

        suffix = escape(footer) if message.parse_mode == "HTML" else footer

        return Message(message.text + suffix, message.parse_mode, message.plain + footer)

    def _render(self, name: str, context: dict[str, object], *, limit: int) -> Message:
        """
        Validate a selected template and render bounded Telegram content.

        Args:
            name: Relative text or HTML template path.
            context: Validated alert fields exposed to Jinja.
            limit: Text or caption budget in UTF-16 code units.

        Returns:
            Formatted content with its plain-text fallback.

        Raises:
            ValueError: The template path or its rendered content is invalid.
        """

        if (
            not re.fullmatch(r"[a-zA-Z0-9_./-]+\.(?:txt|html)\.j2", name)
            or name.startswith("/")
            or any(part in (".", "..") for part in name.split("/"))
        ):
            msg = "invalid template name"
            raise ValueError(msg)

        html = name.endswith(".html.j2")
        environment = self.html_environment if html else self.text_environment
        rendered = environment.get_template(name).render(context).strip()

        if not rendered:
            msg = "empty message"
            raise ValueError(msg)

        if html:
            parser = PlainText()
            parser.feed(rendered)
            plain = "".join(parser.parts).strip()

            if not plain:
                msg = "empty HTML message"
                raise ValueError(msg)

            if len(plain.encode("utf-16-le")) // 2 <= limit:
                return Message(rendered, "HTML", plain)

            return Message(clip(plain, limit), None, clip(plain, limit))

        plain = clip(rendered, limit)
        return Message(plain, None, plain)
