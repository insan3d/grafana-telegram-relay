"""Verify Jinja context, escaping, template reloads, and safe message fallbacks."""

import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from prometheus_client import REGISTRY

from grafana_telegram_relay.messages import MessageTemplates
from tests.factories import alert
lazy from grafana_telegram_relay.models import Alert, AlertStatus


class TemplateTests(unittest.TestCase):
    """Exercise isolated template directories without Telegram or Grafana."""

    @override
    def setUp(self) -> None:
        """Create an isolated template directory and formatter."""

        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.templates = MessageTemplates(self.root)

    @override
    def tearDown(self) -> None:
        """Remove the temporary template directory."""

        self.directory.cleanup()

    def write(self, name: str, content: str) -> None:
        """Write a template into the isolated search directory."""

        (self.root / name).write_text(content)

    def item(self, template: str | None = None, status: AlertStatus = "firing") -> Alert:
        """Build a validated alert and optionally select its message template."""

        item = alert(status)

        if template:
            item.annotations["telegram_template"] = template

        return item

    def test_full_alert_context_retains_grafana_json_field_names(self) -> None:
        """Keep bracket access and camel-case Grafana fields available to templates."""

        self.write(
            "raw.txt.j2", "{{ alert['startsAt'] }} {{ alert['labels']['alertname'] }} {{ alert['values']['A'] }}"
        )

        assert self.templates(self.item("raw.txt.j2")).text == "2026-10-17T12:00:00+00:00 GrafanaTest 1"

    def test_default_message_includes_firing_and_resolved_details(self) -> None:
        """Verify default message includes firing and resolved details."""

        firing = self.templates(self.item())
        assert "FIRING" in firing.text
        assert "GrafanaTest" in firing.text
        assert "instance: grafana" in firing.text
        assert "Test incident" in firing.text
        assert firing.parse_mode is None
        resolved = self.templates(self.item(status="resolved"))
        assert "RESOLVED" in resolved.text
        assert "Resolved:" in resolved.text

    def test_annotation_selects_a_template_with_incident_context(self) -> None:
        """Use the annotation to select a template and expose computed incident details."""

        self.write(
            "database.txt.j2",
            "{{ status }} {{ labels.alertname }} {{ values['A'] }}\n{{ starts_at }} / {{ duration_seconds|int }}s",
        )
        result = self.templates(self.item("database.txt.j2", "resolved"))

        assert result.text == "resolved GrafanaTest 1\n2026-10-17T12:00:00+00:00 / 600s"

    def test_default_can_be_selected_globally(self) -> None:
        """Verify default can be selected globally."""

        self.write("short.txt.j2", "{{ labels.alertname }}: {{ status }}")
        formatter = MessageTemplates(self.root, default="short.txt.j2")
        assert formatter(self.item()).text == "GrafanaTest: firing"

    def test_broken_packaged_default_cannot_prevent_startup_or_delivery(self) -> None:
        """Verify broken packaged default cannot prevent startup or delivery."""

        self.write("default.txt.j2", "{% if syntax_error %}")

        with patch("grafana_telegram_relay.messages.BUILTIN_DIRECTORY", self.root):
            formatter = MessageTemplates(self.root)
            result = formatter(self.item())

        assert "FIRING" in result.text
        assert "GrafanaTest" in result.text
        assert result.parse_mode is None

    def test_edits_are_loaded_without_restarting(self) -> None:
        """Verify edits are loaded without restarting."""

        self.write("custom.txt.j2", "first {{ status }}")
        assert self.templates(self.item("custom.txt.j2")).text == "first firing"
        self.write("custom.txt.j2", "updated message {{ status }}")
        assert self.templates(self.item("custom.txt.j2")).text == "updated message firing"

    def test_bad_or_missing_template_falls_back_without_losing_alert(self) -> None:
        """Verify bad or missing template falls back without losing alert."""

        before = REGISTRY.get_sample_value("telegram_bot_template_failures_total") or 0
        self.write("broken.txt.j2", "{% if status %}")
        self.write("undefined.txt.j2", "{{ not_a_variable.foo }}")
        self.write("empty.txt.j2", "{% if false %}never{% endif %}")
        self.write("runtime.txt.j2", "{{ 1 / 0 }}")
        self.write("default.txt.j2", "{{ also_undefined }}")

        for name in (
            "broken.txt.j2",
            "undefined.txt.j2",
            "missing.txt.j2",
            "empty.txt.j2",
            "runtime.txt.j2",
            "../outside.txt.j2",
            "/etc/passwd",
            "default.txt.j2",
        ):
            result = self.templates(self.item(name))
            assert "GrafanaTest" in result.text
            assert "FIRING" in result.text
            assert result.parse_mode is None

        after = REGISTRY.get_sample_value("telegram_bot_template_failures_total")
        assert after is not None
        assert after - before == 8

    def test_html_escapes_alert_data_and_preserves_authored_tags(self) -> None:
        """Verify html escapes alert data and preserves authored tags."""

        self.write("custom.html.j2", "<b>{{ labels.alertname }}</b>\n{{ annotations.summary }}")
        item = self.item("custom.html.j2")
        item.labels["alertname"] = "CPU <95% & disk>"
        item.annotations["summary"] = '<a href="https://evil.invalid">text</a>'
        result = self.templates(item)
        assert result.parse_mode == "HTML"
        assert "<b>CPU &lt;95% &amp; disk&gt;</b>" in result.text
        assert "&lt;a href=" in result.text
        assert "CPU <95% & disk>" in result.plain

    def test_include_tree_inherits_top_level_escape_mode(self) -> None:
        """Verify include tree inherits top level escape mode."""

        self.write("shared.j2", "{{ annotations.summary }}")
        self.write("shared.txt.j2", "{{ annotations.summary }}")
        self.write("text.txt.j2", "{% include 'shared.j2' %}")
        self.write("html.html.j2", "{% include 'shared.txt.j2' %}")
        item = self.item("text.txt.j2")
        item.annotations["summary"] = "<b>data & value</b>"
        assert self.templates(item).text == "<b>data & value</b>"
        item.annotations["telegram_template"] = "html.html.j2"
        assert self.templates(item).text == "&lt;b&gt;data &amp; value&lt;/b&gt;"

    def test_long_html_degrades_to_valid_plain_text_without_splitting_emoji(self) -> None:
        """Verify long html degrades to valid plain text without splitting emoji."""

        self.write("long.html.j2", "<b>{{ labels.alertname }}</b>\n{{ '🔥' * 1000 }}")
        result = self.templates(self.item("long.html.j2"), limit=1024)
        assert result.parse_mode is None
        assert len(result.text.encode("utf-16-le")) // 2 <= 1024
        assert "GrafanaTest" in result.text
        assert "<b>" not in result.text
        assert "🔥" in result.text

    def test_text_messages_use_full_telegram_text_budget(self) -> None:
        """Verify text messages use full telegram text budget."""

        self.write("long.txt.j2", "{{ 'x' * 2000 }}")
        item = self.item("long.txt.j2")
        assert len(self.templates(item, limit=4096).text) == 2000
        assert len(self.templates(item, limit=1024).text) == 1024

    def test_alert_context_retains_links_and_fingerprint(self) -> None:
        """Expose model links and the incident fingerprint to message templates."""

        item = alert()
        item.generator_url = "https://grafana.example/rule"
        item.silence_url = "https://grafana.example/silence"
        self.write("links.txt.j2", "{{ fingerprint }} {{ generator_url }} {{ silence_url }}")
        item.annotations["telegram_template"] = "links.txt.j2"
        assert self.templates(item).text == "grafana-test https://grafana.example/rule https://grafana.example/silence"
